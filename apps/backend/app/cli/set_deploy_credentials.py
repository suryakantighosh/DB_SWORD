"""Attach split-role deploy credentials to an existing connection.

Usage inside the backend container:

    python -m app.cli.set_deploy_credentials \
        --connection-id caf91a35-03bb-434b-9e9a-3b58c0bc8881 \
        --deploy-username zentrix_deployer \
        --deploy-password "<the-password>" \
        --host app-db \
        --port 5432 \
        --database zentrix_db \
        --sslmode disable

The elevated DSN is Fernet-encrypted with the same
CONNECTION_ENCRYPTION_KEY as the monitoring DSN and stored in
database_connections.encrypted_deploy_connection_string. Monitoring
credentials are untouched.
"""

from __future__ import annotations

import argparse
import asyncio
import uuid
from urllib.parse import quote

from sqlalchemy import select

from app.core.security import encrypt_connection_string
from app.db.session import async_session_factory
from app.models.connection import DatabaseConnection


def _build_dsn(user: str, password: str, host: str, port: int,
               database: str, sslmode: str) -> str:
    up = f"{quote(user, safe='')}:{quote(password, safe='')}"
    return f"postgresql://{up}@{host}:{port}/{database}?sslmode={sslmode}"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--connection-id", required=True)
    ap.add_argument("--deploy-username", required=True)
    ap.add_argument("--deploy-password", required=True)
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, default=5432)
    ap.add_argument("--database", required=True)
    ap.add_argument("--sslmode", default="disable")
    args = ap.parse_args()

    dsn = _build_dsn(
        args.deploy_username, args.deploy_password,
        args.host, args.port, args.database, args.sslmode,
    )
    encrypted = encrypt_connection_string(dsn)

    async with async_session_factory() as db:
        record = await db.scalar(
            select(DatabaseConnection).where(DatabaseConnection.id == uuid.UUID(args.connection_id))
        )
        if not record:
            print(f"connection {args.connection_id} not found")
            return 1
        record.encrypted_deploy_connection_string = encrypted
        record.deploy_username = args.deploy_username
        await db.commit()
        print(f"attached deploy credentials to connection {record.id} ({record.name})")
        print(f"  deploy_username = {args.deploy_username}")
        print(f"  encrypted_deploy_connection_string = <{len(encrypted)} bytes>")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
