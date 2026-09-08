"""Ephemeral Shadow Database management tool.

Clones monitored customer PostgreSQL databases into isolated ephemeral
Docker containers using pg_dump/pg_restore or schema/data copies to enable
safe, paired workload replay and statistical verification without risk to
production.

Reference: ARCHITECTURE.md §4, §8 & PRD.md §5 Feature 2.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import asyncpg

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_ALLOWED_SHADOW_SQL = (
    re.compile(
        r"^\s*CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?"
        r"(?:IF\s+NOT\s+EXISTS\s+)?[A-Za-z_][A-Za-z0-9_$]*\s+ON\s+"
        r"[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)?\s*\("
        r"[A-Za-z_][A-Za-z0-9_$]*(?:\s*,\s*[A-Za-z_][A-Za-z0-9_$]*)*\)\s*;?\s*$",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*ANALYZE(?:\s+[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)?)?\s*;?\s*$", re.IGNORECASE),
    re.compile(r"^\s*VACUUM\s+ANALYZE\s+[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)?\s*;?\s*$", re.IGNORECASE),
)


class ShadowProvisioningError(RuntimeError):
    """Raised when an ephemeral shadow container cannot be provisioned."""


@dataclass
class ShadowConfig:
    image: str = field(default_factory=lambda: get_settings().SHADOW_DB_IMAGE)
    container_prefix: str = "zentrix-shadow"
    postgres_user: str = "postgres"
    postgres_password: str = "shadowpass"
    postgres_db: str = "shadow_test"
    host: str = field(default_factory=lambda: get_settings().SHADOW_DB_HOST)
    port: int | None = None
    memory_limit: str = "2g"
    startup_timeout_seconds: float = 30.0
    mode: str = "full_clone"  # 'full_clone', 'schema_only', 'sampled'


@dataclass
class ShadowDatabase:
    container_id: str
    container_name: str
    port: int
    dsn: str
    is_ready: bool = False

    async def connect(self) -> asyncpg.Connection:
        """Establish a direct asyncpg connection to the shadow container."""
        return await asyncpg.connect(self.dsn, timeout=10.0)


def is_docker_available() -> bool:
    """Check if the Docker CLI is installed and running on the host system."""
    if not shutil.which("docker"):
        return False
    try:
        res = subprocess.run(
            ["docker", "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3.0,
        )
        return res.returncode == 0
    except Exception:
        return False


def _find_free_port(start_port: int = 15432, max_attempts: int = 100) -> int:
    """Find an available local TCP port for the shadow container."""
    import socket

    for port in range(start_port, start_port + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise ShadowProvisioningError("No free local port found for shadow database")


async def wait_for_postgres_ready(
    dsn: str,
    timeout_seconds: float = 30.0,
    interval_seconds: float = 0.5,
) -> bool:
    """Poll the shadow database until it accepts connections or times out."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            conn = await asyncpg.connect(dsn, timeout=2.0)
            await conn.fetchval("SELECT 1")
            await conn.close()
            return True
        except Exception:
            await asyncio.sleep(interval_seconds)
    return False


async def provision_shadow_db(
    config: ShadowConfig | None = None,
) -> ShadowDatabase:
    """Provision a shadow database.

    Model B (default): connect to the long-lived `shadow-pool` Postgres and
    `CREATE DATABASE shadow_<uuid>` for this experiment. Returns a
    ShadowDatabase pointing at the new empty DB. Caller must call
    `clone_customer_database` next to populate it, then `teardown_shadow_db`
    on the returned `container_id` (which is the DB name, not a container id).

    Legacy (opt-in via SHADOW_DB_USE_DOCKER=1): docker-in-docker container
    per experiment — unreliable on Windows Docker Desktop.

    Fallback (opt-in via SHADOW_DB_USE_FAULT_LAB=1): route at the
    fault-lab-db service. Only useful for the pre-Model-B fault-lab
    integration tests; leaves verification at INSUFFICIENT_DATA for any
    customer schema not in fault-lab.
    """
    cfg = config or ShadowConfig()
    use_docker = os.getenv("SHADOW_DB_USE_DOCKER", "").lower() in {"1", "true", "yes"}
    use_fault_lab = os.getenv("SHADOW_DB_USE_FAULT_LAB", "").lower() in {"1", "true", "yes"}

    # ---- Model B shadow-pool (default) ----
    if not use_docker and not use_fault_lab:
        pool_host = os.getenv("SHADOW_POOL_HOST", "shadow-pool")
        pool_port = int(os.getenv("SHADOW_POOL_PORT", "5432"))
        pool_user = os.getenv("SHADOW_POOL_USER", "shadow_admin")
        pool_password = os.getenv("SHADOW_POOL_PASSWORD", "shadow_pool_dev_password")
        pool_admin_db = os.getenv("SHADOW_POOL_ADMIN_DB", "shadow_admin")

        shadow_db_name = f"shadow_{uuid.uuid4().hex[:12]}"
        admin_dsn = (
            f"postgresql://{pool_user}:{pool_password}@{pool_host}:{pool_port}/{pool_admin_db}"
        )
        shadow_dsn = (
            f"postgresql://{pool_user}:{pool_password}@{pool_host}:{pool_port}/{shadow_db_name}"
        )

        logger.info(
            "Provisioning shadow database on shadow-pool",
            extra={"shadow_db_name": shadow_db_name, "host": pool_host, "port": pool_port},
        )
        # Connect to the admin DB and CREATE DATABASE. Uses autocommit because
        # CREATE DATABASE cannot run inside a transaction block.
        try:
            admin_conn = await asyncpg.connect(admin_dsn, timeout=10.0)
        except Exception as exc:
            raise ShadowProvisioningError(
                f"Cannot reach shadow-pool at {pool_host}:{pool_port}: {exc}"
            ) from exc
        try:
            await admin_conn.execute(f'CREATE DATABASE "{shadow_db_name}"')
        finally:
            await admin_conn.close()

        # Sanity: newly created DB should be immediately connectable.
        ready = await wait_for_postgres_ready(shadow_dsn, timeout_seconds=10.0)
        if not ready:
            # Try to drop the orphan and raise.
            await _drop_shadow_db(admin_dsn, shadow_db_name)
            raise ShadowProvisioningError(
                f"Shadow database {shadow_db_name} did not become ready in time"
            )

        return ShadowDatabase(
            container_id=shadow_db_name,       # audit label — the DB name
            container_name=f"shadow-pool/{shadow_db_name}",
            port=pool_port,
            dsn=shadow_dsn,
            is_ready=True,
        )

    # ---- Fault-lab fallback (opt-in via SHADOW_DB_USE_FAULT_LAB=1) ----
    if use_fault_lab:
        shadow_host = "fault-lab-db"
        shadow_port = 5432
        dsn = (
            f"postgresql://fault_lab:fault_lab_dev_password"
            f"@{shadow_host}:{shadow_port}/fault_lab"
        )
        logger.warning(
            f"SHADOW_DB_USE_FAULT_LAB set — routing at bundled fault-lab-db "
            f"({shadow_host}:{shadow_port}). Verification will INSUFFICIENT_DATA "
            "for any customer schema not present in fault-lab."
        )
        return ShadowDatabase(
            container_id="fault-lab-static",
            container_name="zentrix-fault-lab-db-1",
            port=shadow_port,
            dsn=dsn,
            is_ready=True,
        )

    # ---- Original Docker-based provisioning (opt-in via SHADOW_DB_USE_DOCKER=1) ----
    if not is_docker_available():
        raise ShadowProvisioningError(
            "Docker is not available or running. Cannot provision shadow database container."
        )

    unique_id = uuid.uuid4().hex[:8]
    container_name = f"{cfg.container_prefix}-{unique_id}"
    port = cfg.port or _find_free_port()
    dsn = f"postgresql://{cfg.postgres_user}:{cfg.postgres_password}@{cfg.host}:{port}/{cfg.postgres_db}"

    cmd = [
        "docker", "run", "-d",
        "--name", container_name,
        "-p", f"{port}:5432",
        "-e", f"POSTGRES_USER={cfg.postgres_user}",
        "-e", f"POSTGRES_PASSWORD={cfg.postgres_password}",
        "-e", f"POSTGRES_DB={cfg.postgres_db}",
        "-m", cfg.memory_limit,
        cfg.image,
    ]

    logger.info(f"Provisioning shadow container: {container_name} on port {port}")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err_msg = stderr.decode().strip()
            raise ShadowProvisioningError(f"docker run failed: {err_msg}")

        container_id = stdout.decode().strip()
        ready = await wait_for_postgres_ready(dsn, timeout_seconds=cfg.startup_timeout_seconds)
        if not ready:
            await teardown_shadow_db(container_name)
            raise ShadowProvisioningError(
                f"Shadow database failed to become ready within {cfg.startup_timeout_seconds}s"
            )

        return ShadowDatabase(
            container_id=container_id,
            container_name=container_name,
            port=port,
            dsn=dsn,
            is_ready=True,
        )
    except Exception as exc:
        logger.error(f"Error provisioning shadow database: {exc}")
        if not isinstance(exc, ShadowProvisioningError):
            raise ShadowProvisioningError(f"Failed to provision shadow database: {exc}") from exc
        raise




async def _drop_shadow_db(admin_dsn: str, shadow_db_name: str) -> bool:
    """Terminate active connections and DROP DATABASE on the shadow-pool.

    Called both from provision_shadow_db (cleanup on wait_for_postgres_ready
    timeout) and teardown_shadow_db (end-of-experiment).
    """
    try:
        conn = await asyncpg.connect(admin_dsn, timeout=10.0)
    except Exception as exc:
        logger.warning(f"Cannot reach shadow-pool to drop {shadow_db_name}: {exc}")
        return False
    try:
        # Boot any lingering connections to the shadow DB so DROP can proceed.
        await conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            shadow_db_name,
        )
        await conn.execute(f'DROP DATABASE IF EXISTS "{shadow_db_name}"')
        return True
    except Exception as exc:
        logger.warning(f"Failed to DROP DATABASE {shadow_db_name}: {exc}")
        return False
    finally:
        await conn.close()

async def teardown_shadow_db(container_id_or_name: str) -> bool:
    """Tear down a shadow environment.

    Handles three shapes of `container_id_or_name`:
      - "shadow_<hex>"        Model-B shadow-pool DB — DROP DATABASE on shadow-pool.
      - "fault-lab-static"    Legacy pre-Model-B bypass — never removed.
      - anything else         Legacy docker container name — `docker rm -f`.
    """
    # Model B: shadow-pool-managed named database.
    if container_id_or_name.startswith("shadow_"):
        pool_host = os.getenv("SHADOW_POOL_HOST", "shadow-pool")
        pool_port = int(os.getenv("SHADOW_POOL_PORT", "5432"))
        pool_user = os.getenv("SHADOW_POOL_USER", "shadow_admin")
        pool_password = os.getenv("SHADOW_POOL_PASSWORD", "shadow_pool_dev_password")
        pool_admin_db = os.getenv("SHADOW_POOL_ADMIN_DB", "shadow_admin")
        admin_dsn = (
            f"postgresql://{pool_user}:{pool_password}@{pool_host}:{pool_port}/{pool_admin_db}"
        )
        logger.info(f"Tearing down shadow database {container_id_or_name} on shadow-pool")
        return await _drop_shadow_db(admin_dsn, container_id_or_name)

    # Persistent bundled shadow — never remove.
    if container_id_or_name in {"fault-lab-static", "zentrix-fault-lab-db-1"}:
        return True

    # Legacy Docker-container teardown path.
    if not is_docker_available():
        return False
    logger.info(f"Tearing down shadow container: {container_id_or_name}")
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "rm", "-f", container_id_or_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        return proc.returncode == 0
    except Exception as exc:
        logger.warning(f"Failed to remove shadow container: {exc}")
        return False


async def clone_customer_database(source_dsn: str, target_dsn: str) -> None:
    """Clone a customer PostgreSQL database into the fresh shadow database.

    Runs `pg_dump --format=custom | pg_restore` against the two DSNs over the
    Docker network. Client binaries live in the backend image
    (`postgresql-client` apt package). Credentials are never logged.
    A failed dump or restore aborts the experiment instead of falling back
    to synthetic metrics.
    """
    # Legacy skip — Model B shadow-pool DBs are named shadow_<uuid>, not fault_lab.
    # Kept so SHADOW_DB_USE_FAULT_LAB=1 opt-in path still no-ops.
    if "/fault_lab" in target_dsn.lower():
        logger.info(
            "clone_customer_database: skipping clone into static fault-lab shadow "
            "(target_dsn=%s)", target_dsn.split("@")[-1] if "@" in target_dsn else "<hidden>"
        )
        return
    if not shutil.which("pg_dump") or not shutil.which("pg_restore"):
        raise ShadowProvisioningError(
            "pg_dump and pg_restore are required to create a real shadow clone"
        )

    dump = await asyncio.create_subprocess_exec(
        "pg_dump",
        "--format=custom",
        "--no-owner",
        "--no-acl",
        "--dbname",
        source_dsn,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    restore = await asyncio.create_subprocess_exec(
        "pg_restore",
        "--no-owner",
        "--no-acl",
        "--clean",
        "--if-exists",
        "--exit-on-error",
        "--dbname",
        target_dsn,
        stdin=dump.stdout,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    restore_stdout, restore_stderr = await restore.communicate()
    dump_stderr = await dump.stderr.read() if dump.stderr else b""
    dump_code = await dump.wait()
    if dump_code != 0 or restore.returncode != 0:
        details = (dump_stderr + restore_stderr).decode(errors="replace").strip()
        raise ShadowProvisioningError(f"Shadow clone failed: {details[-2000:]}")


async def _capture_explain_plan(
    connection: asyncpg.Connection, query_text: str
) -> dict[str, Any] | None:
    """Best-effort EXPLAIN (FORMAT JSON) for the Arc A before/after diff card.

    Returns the raw plan JSON (list-of-one shape from Postgres) or None on
    any error. Deliberately does NOT run ANALYZE — the shadow-pool workload
    already exercised the query, and ANALYZE mutations here would skew the
    baseline vs candidate paired measurement.
    """
    if not query_text or not query_text.strip():
        return None
    stripped = query_text.strip().rstrip(";")
    lower = stripped.lower()
    # Only EXPLAIN read-only SELECT/WITH — matches pg_introspection's guard.
    if not (lower.startswith("select") or lower.startswith("with")):
        return None
    try:
        row = await connection.fetchval(f"EXPLAIN (FORMAT JSON) {stripped}")
    except Exception as exc:  # noqa: BLE001 — capture is advisory, never blocks install
        logger.warning("EXPLAIN capture failed for Arc A diff card: %s", exc)
        return None
    if isinstance(row, str):
        import json
        try:
            row = json.loads(row)
        except Exception:  # noqa: BLE001
            return None
    return row


async def install_candidate_optimization(
    connection: asyncpg.Connection,
    candidate_sql: str,
    workload_query: str | None = None,
) -> dict[str, Any]:
    """Execute a candidate optimization (DDL/config) against the shadow database.

    Measures execution time and returns execution metadata. When
    `workload_query` is provided, also captures EXPLAIN (FORMAT JSON) for
    that query before and after the candidate install — the two plans feed
    the Arc A EXPLAIN-diff card on the experiment detail page.
    """
    start_time = time.monotonic()
    cleaned = candidate_sql.strip()
    if not any(pattern.match(cleaned) for pattern in _ALLOWED_SHADOW_SQL):
        return {
            "candidate_sql": candidate_sql,
            "success": False,
            "duration_ms": 0.0,
            "error": "Candidate SQL is outside the supported index/statistics/vacuum action set",
            "explain_before": None,
            "explain_after": None,
        }
    explain_before = await _capture_explain_plan(connection, workload_query) if workload_query else None
    try:
        await connection.execute(candidate_sql)
        duration_ms = (time.monotonic() - start_time) * 1000.0
        explain_after = await _capture_explain_plan(connection, workload_query) if workload_query else None
        return {
            "candidate_sql": candidate_sql,
            "success": True,
            "duration_ms": duration_ms,
            "error": None,
            "explain_before": explain_before,
            "explain_after": explain_after,
        }
    except Exception as exc:
        duration_ms = (time.monotonic() - start_time) * 1000.0
        logger.error(f"Failed to install candidate on shadow database: {exc}")
        return {
            "candidate_sql": candidate_sql,
            "success": False,
            "duration_ms": duration_ms,
            "error": str(exc),
            "explain_before": explain_before,
            "explain_after": None,
        }


async def clone_schema_and_tables(
    source_conn: asyncpg.Connection,
    target_conn: asyncpg.Connection,
    table_names: Sequence[str],
    *,
    sample_limit: int | None = None,
) -> dict[str, Any]:
    """Lightweight in-Python table cloner for test fixtures or sampled shadow runs."""
    cloned_tables = []
    for table in table_names:
        # Get column definitions
        cols = await source_conn.fetch(
            """
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_name = $1
            ORDER BY ordinal_position
            """,
            table,
        )
        if not cols:
            continue

        col_defs = ", ".join(f'"{c["column_name"]}" {c["data_type"]}' for c in cols)
        await target_conn.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({col_defs})')

        # Copy data
        limit_clause = f" LIMIT {sample_limit}" if sample_limit else ""
        rows = await source_conn.fetch(f'SELECT * FROM "{table}"{limit_clause}')
        if rows:
            col_names = [f'"{c["column_name"]}"' for c in cols]
            placeholders = ", ".join(f"${i+1}" for i in range(len(col_names)))
            insert_sql = f'INSERT INTO "{table}" ({", ".join(col_names)}) VALUES ({placeholders})'
            for row in rows:
                await target_conn.execute(insert_sql, *row.values())

        cloned_tables.append(table)

    return {"status": "CLONED", "tables": cloned_tables, "sample_limit": sample_limit}


@asynccontextmanager
async def shadow_environment(
    config: ShadowConfig | None = None,
) -> AsyncGenerator[ShadowDatabase, None]:
    """Async context manager for automatic provisioning and teardown of shadow DB."""
    instance = await provision_shadow_db(config)
    try:
        yield instance
    finally:
        await teardown_shadow_db(instance.container_id)
