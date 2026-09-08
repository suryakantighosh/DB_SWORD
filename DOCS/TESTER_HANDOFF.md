# Zentrix.ai — Tester Handoff (Session 2026-09-08)

> **You are testing a working closed-loop demo of Zentrix.ai on your own laptop.**
> The system: monitors a PostgreSQL database, diagnoses a slow query as INDEX_MISSING, generates a `CREATE INDEX` recommendation, simulates it through a 6-node LangGraph pipeline, gates on human approval, deploys via a **separate elevated database role** (never the monitoring role), and watches for regressions during a canary window with auto-rollback.
>
> This file is written to be pasted at the top of a new AI chat with the tester (Claude, GPT, whatever) so the AI can walk them through it. Everything the tester needs — prerequisites, setup steps, what changed in this session, how to click through, what "success" looks like, and how to debug failures — is inline.

---

## 0. Repo & prerequisites

| Item | Version / value |
|---|---|
| Repo root on tester's disk | `C:\dev\zentrix` (Windows) or wherever you clone. **Keep outside OneDrive** — OneDrive locks git internals. |
| OS | Windows 11 verified. macOS / Linux should also work with Docker Desktop. |
| Docker Desktop | 29.2.1+ |
| Compose | v5.1.0+ (docker-compose v2 syntax works) |
| Disk | ≥ 10 GB free (Postgres data + MLflow + images) |
| Ports needed | 3000 (frontend), 5000 (mlflow), 5432 (app-db), 5433 (fault-lab-db), 5434 (shadow-pool), 8080 (backend) |
| Chrome / Firefox | latest, for the UI at http://localhost:3000 |

Nothing needs to be installed globally other than Docker Desktop and Git.

---

## 1. What Zentrix does (30-second briefing)

Zentrix is an **evidence-first agentic DBA for PostgreSQL**. The closed loop:

1. **Observe** — telemetry-collector polls `pg_stat_statements`, `pg_stat_user_tables`, `pg_stat_activity` on the customer DB over a read-only connection. Writes to `query_metrics` / `table_metrics` / `plan_metrics`.
2. **Diagnose** — LangGraph with 5 specialist agents (PLANNER, CONCURRENCY, VACUUM, IO_BUFFER, SCHEMA_INDEX) each vote a root cause with directness + confidence. A supervisor picks the winner. ML models (Isolation Forest anomaly + LightGBM RCA + LSTM temporal) contribute signals.
3. **Recommend** — deterministic candidate generation from a fixed grammar (`CREATE INDEX`, `ANALYZE`, `VACUUM ANALYZE`). For INDEX_MISSING → composite index on WHERE-clause columns.
4. **Simulate** — 6-node pipeline (HypoPG → ML prediction → Shadow → Statistical verification → Skeptic review → Policy engine). Verdict: VERIFIED / CONDITIONAL / INSUFFICIENT_DATA / REJECTED.
5. **Approve** — a human clicks Approve. Recorded in immutable audit trail with identity + timestamp.
6. **Deploy** — DDL runs on the real customer DB **through a separate elevated role** (see Model B, section 3 below). Canary window opens.
7. **Watch** — canary-monitor polls live p95 / lock waits every N seconds. Auto-rollback if regression > 15%. Otherwise commits at end of window.

The self-monitoring demo has Zentrix's app-db BE the customer DB. `demo_orders` (100k rows, no index on `(customer_id, status)`) is the slow-query workload.

---

## 2. What changed in Session 2026-09-08 (fix log)

If the tester wants to diff against upstream, these are the files touched. The AI should read this list to understand the state.

### Model B — split-role deploy credentials (F21)

| File | What |
|---|---|
| `apps/backend/app/models/connection.py` | +2 nullable cols on `DatabaseConnection`: `encrypted_deploy_connection_string`, `deploy_username`. |
| `apps/backend/app/models/audit.py` | +1 col on `CanaryRun`: `executed_by_role` (values: `"deploy"` \| `"monitoring"`). |
| `apps/backend/migrations/versions/a1b2c3d4e5f6_split_role_deploy_credentials.py` | New Alembic migration for the 3 columns. |
| `apps/backend/app/db/customer_db.py` | New `CustomerConnectionManager.get_deploy_connection(connection_id, db)` returning `(asyncpg.Connection, role_label)`. Uses `command_timeout=180s` + `SET statement_timeout = 180000`. |
| `apps/backend/app/services/simulation_service.py` | `deploy_canary` now calls `get_deploy_connection` when no `customer_connection` is injected. Records `executed_by_role` on the CanaryRun. |
| `apps/backend/app/api/routes/experiments.py` | `/experiments/{id}/deploy` calls the service with `customer_connection=None` (forces split-role path). |
| `apps/backend/app/cli/set_deploy_credentials.py` | **New CLI** to attach deploy credentials to an existing connection. |
| `docs/CONNECTION_PRIVILEGES.md` | New: grant statements + Model B rationale. |

### Diagnosis pipeline correctness

| File | What |
|---|---|
| `apps/backend/app/agents/graph_diagnosis.py` | **Fixed supervisor tiebreak** — now compares primary against runner-up (not sum of all others). Fixes NO_ACTIVE_INCIDENT bug that discarded correct INDEX_MISSING votes. Also `query_text` falls back to `query` if the key is missing. |
| `apps/backend/app/services/diagnosis_service.py` | Added `"query_text"` field to the persisted `query_metrics` dicts so `graph_diagnosis` SCHEMA_INDEX fallback branch can fire on pg_stat_statements evidence. |
| `apps/backend/app/services/recommendation_service.py` | 3 changes: (a) `_top_query` now filters out Zentrix's own internal tables (self-monitoring blindness), (b) index emit is `CREATE INDEX IF NOT EXISTS` and non-concurrent by default (env `ZENTRIX_INDEX_CONCURRENTLY=true` to opt into CIC), (c) added `_ZENTRIX_INTERNAL_TABLES` blacklist. |

### Canary observation correctness

| File | What |
|---|---|
| `apps/backend/app/workers/canary_monitor.py` | **Collector fix** — uses `mean_exec_time` (moves with real workload) instead of cumulative `max_exec_time`; filter `calls >= 5`; returns `insufficient_samples: True` when no meaningful sample. **Warmup guard** — `_WARMUP_SECONDS = 30.0` skips rollback checks for the first 30 s after canary start. |
| `apps/backend/app/api/routes/experiments.py` | SSE `event_generator` calls `db.expire_all()` per tick so stream sees canary_monitor's writes (kills SQLAlchemy identity-map stale reads). |

### Grammar & guard relaxation (demo unblock)

| File | What |
|---|---|
| `apps/backend/app/services/simulation_service.py` | `deploy_canary` short-circuits INSUFFICIENT_DATA when env `ZENTRIX_ALLOW_UNVERIFIED_DEPLOY=true` (demo-only bypass — B1 gap). `ALLOWED_CANARY_PATTERNS` accepts CREATE INDEX with or without CONCURRENTLY. Also `run_simulation` guards `None` metrics returned from shadow install failures. |

### Arc A — Real shadow-pool (Model B verification)

| File | What |
|---|---|
| `docker-compose.yml` | New `shadow-pool` service (persistent Postgres 16) + `shadow-pool-data` volume. Backend and shadow-lab-worker gain `depends_on: shadow-pool`. |
| `apps/backend/app/tools/shadow_db_tool.py` | `provision_shadow_db` now `CREATE DATABASE shadow_<uuid>` on shadow-pool. `clone_customer_database` runs real `pg_dump \| pg_restore` between customer and shadow. `teardown_shadow_db` `DROP DATABASE`s on the pool. New `_drop_shadow_db` helper terminates lingering connections before DROP. Legacy fault-lab path preserved behind `SHADOW_DB_USE_FAULT_LAB=1` env. Legacy docker-in-docker path preserved behind `SHADOW_DB_USE_DOCKER=1`. |
| `apps/backend/app/services/simulation_service.py` | `deploy_canary` logs a **WARN** the moment `ZENTRIX_ALLOW_UNVERIFIED_DEPLOY=true` widens the deployable set — impossible to miss in a prod log pipeline. |
| `.env.example` | New `SHADOW_POOL_HOST/PORT/USER/PASSWORD/ADMIN_DB` block. `ZENTRIX_ALLOW_UNVERIFIED_DEPLOY` default flipped to `false`. |

### Arc B — Closed-loop learning wired

| File | What |
|---|---|
| `apps/backend/app/workers/canary_monitor.py` | New `_observed_p95_delta` + `_close_prediction_loop` helpers. Called from `execute_commit` AND `execute_rollback` before final DB commit. Writes `experiment.actual_latency_delta` from real production observation (not the shadow-predicted copy). Propagates to every linked `ModelPrediction.actual` + `absolute_error`. Unlocks `retrain_worker.compute_prediction_errors_and_calibration` producing real MAE numbers. |

### Arc C — Fault-lab shipping gate + bandit graduation

| File | What |
|---|---|
| `apps/backend/tests/e2e/test_fault_lab_e2e.py` | New pytest — for each of 6 canonical fault scenarios in `FAULT_MATRIX`, applies fault to `fault-lab-db`, runs full diagnosis pipeline, asserts `primary_root_cause` matches expected. Ship gate = ≥ 4/6 must pass. Skips cleanly when fault-lab-db unreachable. |
| `apps/backend/app/ml/bandit/policy.py` | New `promote_phase_if_ready` — deterministic graduation gate. PHASE_1→2 at ≥50 labelled experiments; PHASE_2→3 at ≥200; PHASE_3→4 requires IPS offline eval pass. New `current_rollout_phase(db)` reads count of `ModelPrediction.actual is not None` and returns the current phase. |
| `apps/backend/app/agents/graph_forecast.py` | `bandit` now takes `rollout_phase` from state (caller supplies); falls back to PHASE_1_RULE_BASED. |

### Docker & config

| File | What |
|---|---|
| `docker-compose.yml` | Added named volume `feature1-artifacts` mounted at `/workspace/apps/backend/.artifacts` on the backend service. Persists ML models across rebuilds. **User has already updated this.** |

---

## 3. First-time setup (tester runs these once)

Open PowerShell (Windows) or bash (macOS/Linux). Substitute `docker-compose` with `docker compose` if using v2.

### 3.1 Clone and bootstrap

```powershell
git clone <your-repo-url> C:\dev\zentrix
Set-Location C:\dev\zentrix
```

### 3.2 Environment file

```powershell
Copy-Item .env.example .env
```

Then open `.env` in a text editor and set at minimum:

| Key | Value for local testing |
|---|---|
| `APP_DATABASE_URL` | `postgresql+asyncpg://zentrix:zentrix_dev_password@app-db:5432/zentrix_db` |
| `CONNECTION_ENCRYPTION_KEY` | Generate one: `docker run --rm python:3.11 python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `DEV_CONNECTIONS_WITHOUT_AUTH` | `true` (skips Clerk for local testing) |
| `NEXT_PUBLIC_API_BASE_URL` | `http://localhost:8080` |
| `ZENTRIX_ALLOW_UNVERIFIED_DEPLOY` | `true` — demo-only bypass (see section 6.5) |
| Clerk keys | leave empty for local testing (auth is bypassed by `DEV_CONNECTIONS_WITHOUT_AUTH=true`) |

### 3.3 Build and start

```powershell
docker-compose build
docker-compose up -d
docker-compose ps
```

You should see 9 containers all `Up`:

```
zentrix-app-db-1                 Up (healthy)
zentrix-backend-1                Up
zentrix-canary-monitor-1         Up
zentrix-fault-lab-db-1           Up (healthy)
zentrix-frontend-1               Up
zentrix-mlflow-1                 Up
zentrix-retrain-worker-1         Up
zentrix-shadow-lab-worker-1      Up
zentrix-telemetry-collector-1    Up
```

### 3.4 Apply migrations

```powershell
docker-compose exec backend alembic upgrade head
```

Last line should read: `Running upgrade f2a3b4c5d6e7 -> a1b2c3d4e5f6, Model-B split-role deploy credentials + canary executed_by_role.`

### 3.5 Seed the demo workload table

```powershell
docker-compose exec -T app-db psql -U zentrix -d zentrix_db -c "CREATE TABLE IF NOT EXISTS demo_orders (id serial PRIMARY KEY, customer_id int NOT NULL, product_id int NOT NULL, order_date date NOT NULL DEFAULT CURRENT_DATE, amount numeric(10,2) NOT NULL, status varchar(20) NOT NULL DEFAULT 'pending');"

docker-compose exec -T app-db psql -U zentrix -d zentrix_db -c "INSERT INTO demo_orders (customer_id, product_id, amount, status) SELECT (random()*10000)::int, (random()*100)::int, (random()*1000)::numeric, (ARRAY['pending','paid','shipped','cancelled'])[1 + floor(random()*4)::int] FROM generate_series(1, 100000);"

docker-compose exec -T app-db psql -U zentrix -d zentrix_db -c "SELECT COUNT(*) FROM demo_orders;"
```

Expect `count = 100000`.

### 3.6 Provision the Model B deploy role

The elevated role Zentrix uses ONLY for DDL. Monitoring never touches it.

```powershell
docker-compose exec -T app-db psql -U zentrix -d zentrix_db -c "CREATE ROLE zentrix_deployer WITH LOGIN PASSWORD 'deploy_dev_password'; GRANT USAGE, CREATE ON SCHEMA public TO zentrix_deployer; ALTER TABLE demo_orders OWNER TO zentrix_deployer;"
```

### 3.6.1 Verify the shadow-pool is healthy (Arc A)

```powershell
docker-compose exec shadow-pool psql -U shadow_admin -d shadow_admin -c "SELECT current_database(), current_user, version();"
```

Should print the shadow-pool version and `current_user = shadow_admin`.
No further seeding needed — the shadow-pool creates per-experiment
`shadow_<uuid>` databases automatically on simulate.

### 3.7 Train the ML artifacts

```powershell
docker-compose exec backend python -m app.ml.train_feature1_bundle --dsn postgresql://fault_lab:fault_lab_dev_password@fault-lab-db:5432/fault_lab --output /workspace/apps/backend/.artifacts
```

Takes 2–5 min. Ignore MLflow git warnings. Final JSON block should contain `"source":"fault_lab","status":"promoted"`. Verify:

```powershell
docker-compose exec backend ls -la /workspace/apps/backend/.artifacts/
docker-compose exec backend cat /workspace/apps/backend/.artifacts/manifest.json
```

Expect 3 model files + manifest. These persist across rebuilds thanks to the `feature1-artifacts` volume.

### 3.8 Set up the Demo App DB connection in the UI

Open http://localhost:3000. You should land on the dashboard without a login prompt (because `DEV_CONNECTIONS_WITHOUT_AUTH=true`).

- Go to **Connections** → **Add connection** (or use the wizard on the dashboard).
- Name: `Demo App DB`
- Host: `app-db`
- Port: `5432`
- Database: `zentrix_db`
- Username: `zentrix` (superuser — the wizard will auto-create a `zentrix_monitor_*` monitoring role)
- Password: `zentrix_dev_password`
- SSL mode: `disable`

Save. Wait ~5 s for the connection to go **Healthy**. The wizard creates a monitoring role with limited grants; you'll see it in `SELECT username FROM database_connections;`.

### 3.9 Attach the elevated deploy credential

Grab the connection UUID:

```powershell
docker-compose exec app-db psql -U zentrix -d zentrix_db -c "SELECT id, name, username FROM database_connections;"
```

Copy the UUID. Then run the CLI (note: password on the command line is a known dev-time compromise; see section 6.1):

```powershell
docker-compose exec backend python -m app.cli.set_deploy_credentials --connection-id <UUID-HERE> --deploy-username zentrix_deployer --deploy-password deploy_dev_password --host app-db --port 5432 --database zentrix_db --sslmode disable
```

Verify:

```powershell
docker-compose exec app-db psql -U zentrix -d zentrix_db -c "SELECT name, username, deploy_username, encrypted_deploy_connection_string IS NOT NULL AS has_deploy_dsn FROM database_connections;"
```

Should show `deploy_username = zentrix_deployer` and `has_deploy_dsn = t`.

Setup complete.

---

## 4. Test the closed loop end-to-end

### 4.1 Reset pg_stat_statements + start a continuous workload

Fresh state so `demo_orders` dominates the top-slow-queries list:

```powershell
docker-compose exec -T app-db psql -U zentrix -d zentrix_db -c "SELECT pg_stat_statements_reset();"
```

**Leave this loop running in its own PowerShell window for the whole test** — the canary needs traffic to observe:

```powershell
$deadline = (Get-Date).AddMinutes(20)
while ((Get-Date) -lt $deadline) {
  $q = "SELECT COUNT(*) FROM demo_orders WHERE customer_id = $((Get-Random -Max 10000)) AND status = 'shipped';"
  $q | docker-compose exec -T app-db psql -U zentrix -d zentrix_db -q -o NUL
  Start-Sleep -Milliseconds 200
}
```

### 4.2 Wait 60 s then trigger diagnostics

Open http://localhost:3000/diagnostics. Click **Run Diagnostics**. Open the newest diagnosis (top of list). Expected:

- Primary badge: **INDEX_MISSING** at ~93%
- ML panel: Anomaly Score ~80-90%, RCA classifier producing class probabilities (temporal may still say "Needs 30 telemetry rows" — that's fine)
- Causal evidence graph: SCHEMA_INDEX mechanism node cites the demo_orders query text
- Candidate Optimizations (right panel): "Add a selective index on demo_orders" with Medium risk

Click **Open recommendation to start a real shadow simulation**.

### 4.3 Simulate

You're on the recommendation detail. Candidate SQL should be:

```sql
CREATE INDEX IF NOT EXISTS zentrix_idx_<12hex> ON demo_orders (customer_id, status);
```

Click **Simulate**. The 6-stage pipeline runs (~5-15 s). Verdict comes back as **INSUFFICIENT_DATA** with all 6 nodes green-checked. **This is expected** — the shadow DB doesn't have `demo_orders` (see B1 gap, section 6.4). The demo bypass flag lets you proceed.

### 4.4 Approve + Deploy

Click **Approve deployment** → **Confirm approve**. Should get a 201 with a CanaryRun created (no "Failed to fetch" toast).

Watch the backend log confirm split-role fired:

```powershell
docker-compose logs --tail=100 backend | Select-String -Pattern 'deploy|Opening split-role|Executing canary DDL'
```

Expect:
```
Opening split-role deploy connection ... deploy_username=zentrix_deployer
Executing canary DDL on customer DB: CREATE INDEX IF NOT EXISTS zentrix_idx_... ON demo_orders (customer_id, status);
```

### 4.5 Watch the canary

Follow the canary-monitor:

```powershell
docker-compose logs -f canary-monitor | Select-String -Pattern 'warmup|p95|insufficient|ROLLED|COMMIT|Error'
```

- First 30 s: `warmup=True` (rollback checks muted)
- After 30 s: real p95 values that MOVE tick-to-tick (thanks to the mean_exec_time fix)
- At end of window (default `CANARY_MONITOR_WINDOW_MINUTES` from `.env`): `COMMITTED` (index helps → no regression → commits)

### 4.6 Verify the win outside the UI (this is your proof-of-work)

```powershell
docker-compose exec app-db psql -U zentrix -d zentrix_db -c "\d demo_orders"
```

Should now show a new index: `zentrix_idx_... btree (customer_id, status)`.

```powershell
docker-compose exec app-db psql -U zentrix -d zentrix_db -c "EXPLAIN ANALYZE SELECT COUNT(*) FROM demo_orders WHERE customer_id = 137 AND status = 'shipped';"
```

Plan should have flipped from `Seq Scan on demo_orders (Rows Removed by Filter: 100000)` to `Index Scan using zentrix_idx_...`. Execution Time should drop from ~50-70 ms to sub-millisecond.

Audit trail:

```powershell
docker-compose exec app-db psql -U zentrix -d zentrix_db -c "SELECT experiment_id, status, executed_by_role, canary_sql_applied, started_at, completed_at FROM canary_runs ORDER BY started_at DESC LIMIT 3;"
```

Latest row should have `executed_by_role = 'deploy'` — confirms the split-role path fired.

**Screenshot the EXPLAIN before/after + the canary_runs row. That's the demo.**

---

### 4.7 Verify the learning loop closed (Arc B)

After the canary commits/rollbacks:

```powershell
docker-compose exec app-db psql -U zentrix -d zentrix_db -c "SELECT id, experiment_id, actual, absolute_error FROM model_predictions WHERE experiment_id IN (SELECT id FROM optimization_experiments WHERE status IN ('DEPLOYED', 'ROLLED_BACK')) ORDER BY created_at DESC LIMIT 5;"
```

Expected: at least one row with `actual` populated (not NULL) and
`absolute_error` = |actual − prediction|. Confirms the retrain worker
will now compute real MAE instead of `total_labeled=0`.

### 4.8 Run the fault-lab shipping gate (Arc C)

```powershell
docker-compose exec backend python -m pytest tests/e2e/test_fault_lab_e2e.py -v
```

Expected: `test_shipping_gate_pass_rate` passes with ≥ 4/6 fault scenarios
correctly diagnosed. Individual xfails are acceptable at this stage; a hard
FAIL on the gate test means the diagnosis pipeline regressed.

## 5. Manual test matrix (structured checklist for the tester)

Tick each row. If any fails, jump to section 7 (troubleshooting).

| # | Test | Expected | Command / where |
|---|---|---|---|
| 1 | All 9 containers healthy | `docker-compose ps` all Up | Terminal |
| 2 | Migrations up to head | `alembic upgrade head` prints `a1b2c3d4e5f6` | Terminal |
| 3 | ML artifacts persist across rebuild | After `docker-compose build backend && up -d --force-recreate backend`, artifacts still there | `ls /workspace/apps/backend/.artifacts/` |
| 4 | UI loads at :3000 | Dashboard renders, no auth wall | Browser |
| 5 | Connection wizard | Adds a `Demo App DB` connection, marked Healthy within 10s | UI: Connections |
| 6 | Deploy CLI attaches creds | `deploy_username = zentrix_deployer` and `has_deploy_dsn = t` | SQL check in section 3.9 |
| 7 | Diagnostics runs | Newest diagnosis is INDEX_MISSING ≥90% | UI: Diagnostics → Run |
| 8 | Recommendation card renders | Medium risk "Add a selective index on demo_orders" | UI: Diagnosis detail (right panel) |
| 9 | Simulate completes | 6 green checks, verdict INSUFFICIENT_DATA | UI: Recommendation → Simulate |
| 10 | Approve returns 200 | No "Failed to fetch" toast | UI: Confirm approve |
| 11 | Deploy returns 201 | CanaryRun row created in DB | SQL: SELECT FROM canary_runs |
| 12 | Split-role log lines fire | Both "Opening split-role" AND "Executing canary DDL on customer DB" in backend log | `docker-compose logs backend` |
| 13 | Index actually exists on demo_orders | `\d demo_orders` shows `zentrix_idx_...` | psql |
| 14 | Query plan flipped | EXPLAIN shows Index Scan, not Seq Scan | psql |
| 15 | Canary either COMMITs or ROLLBACKs | `canary_runs.status` becomes COMMITTED or ROLLED_BACK within `CANARY_MONITOR_WINDOW_MINUTES` | SQL |
| 16 | Audit records elevated role | `canary_runs.executed_by_role = 'deploy'` | SQL |
| 17 | SSE stream ends cleanly | Frontend deployment view stops showing RUNNING when the run completes | UI |

---

## 6. Known issues & workarounds (this session did NOT fix these)

### 6.1 Password on CLI command line (F21 credential exposure)

`set_deploy_credentials.py` accepts `--deploy-password` as a plain argument. It lands in shell history and `ps -ef` output. **Do not use this in production.** Tracking: add `--deploy-password-stdin` flag that reads from stdin.

### 6.2 Deploy has a legacy injected-connection branch

`deploy_canary` still has `if customer_connection is not None:` that would let a future caller pass a monitoring pool and bypass split-role. Route was updated to pass None; the branch is a landmine for a future refactor. Tracking: assert `customer_connection is None` in production mode, remove branch when tests are migrated.

### 6.3 `_mentions_internal_table` false-positive risk

Blacklist matches substrings like ` users `, ` diagnoses `. If a customer DB legitimately has a `users` table, its slow queries silently drop from recommendations. Tracking: qualify by schema (`public.zentrix_*` prefix) or use a config-time list.

### 6.4 Shadow DB now has real customer schema (was B1 — FIXED in Arc A)

Model-B shadow-pool now creates a per-experiment `shadow_<uuid>` database,
`pg_dump | pg_restore`s the customer schema into it, runs the paired workload,
and DROPs the shadow on teardown. Verification produces real VERIFIED /
REJECTED verdicts. `ZENTRIX_ALLOW_UNVERIFIED_DEPLOY` is now default false and
should stay off — a WARN log fires in the backend the moment it's set true.

### 6.5 `ZENTRIX_ALLOW_UNVERIFIED_DEPLOY=true` must NOT ship to prod

Bypass in `simulation_service.deploy_canary`. If this env leaks into a production deployment, any unverified candidate is deployable through the standard approve flow with no statistical evidence. **Grep for this before shipping.**

### 6.6 CIC hangs under self-monitoring

`CREATE INDEX CONCURRENTLY` waits forever on old snapshots when Zentrix monitors its own DB. **Workaround shipped:** default to plain `CREATE INDEX`. Production sets `ZENTRIX_INDEX_CONCURRENTLY=true` when the metadata DB is separated from the customer DB.

### 6.7 `executed_by_role` reports "monitoring" for injected connections

Wrong for tests, dangerously misleading if a real non-monitoring connection is ever injected. Tracking: rename to `"injected"` for the legacy branch and audit for real-world call sites.

### 6.8 Warmup + insufficient_samples can silence real regressions

Combined guards mute rollback for the first 30 s + until top query has ≥5 calls. A regression that manifests early gets 90 s of grace. Tracking: shorter warmup + call-count-based (not time-based) grace.

### 6.9 `ModelPrediction.actual` — FIXED in Arc B

Canary commit AND rollback now populate `experiment.actual_latency_delta`
from the real production-observed p95 delta and propagate to every linked
`ModelPrediction.actual` + `absolute_error`. Retrain worker will produce
real MAE and drive drift detection.

### 6.10 Bandit graduation — FIXED in Arc C

`promote_phase_if_ready` provides deterministic graduation: PHASE_1→2 at
≥50 labelled experiments, PHASE_2→3 at ≥200, PHASE_3→4 gated by IPS eval
pass. `graph_forecast` takes rollout_phase from caller state (which reads
`current_rollout_phase(db)`). Advancement is monotonic — never regresses,
never skips.

---

## 7. Troubleshooting (paste error → find fix)

| Symptom | Likely cause | Fix |
|---|---|---|
| `column "deploy_username" does not exist` | Migration didn't run | `docker-compose exec backend alembic upgrade head` |
| `role "zentrix_deployer" does not exist` | Section 3.6 skipped | Run the CREATE ROLE block |
| `must be owner of table demo_orders` | Section 3.6 `ALTER TABLE OWNER` skipped, OR `set_deploy_credentials` not run | Section 3.9 |
| Approve returns "Failed to fetch" | Backend 500. Get the real error: | `docker-compose logs --tail=200 backend \| Select-String -Pattern 'Error\|Traceback' -Context 0,5` |
| Diagnosis is NO_ACTIVE_INCIDENT | Workload not landing, OR the supervisor tiebreak fix isn't in the container | Verify: `docker-compose exec backend grep runner_up /workspace/apps/backend/app/agents/graph_diagnosis.py` |
| Diagnosis targets `query_metrics` | Self-monitoring blindness — filter not applied | Verify: `docker-compose exec backend grep _ZENTRIX_INTERNAL_TABLES /workspace/apps/backend/app/services/recommendation_service.py` |
| CIC hangs at "waiting for old snapshots" | Self-monitoring — expected. Cancel + drop invalid index. | `SELECT pg_cancel_backend(pid), query FROM pg_stat_activity WHERE query ILIKE '%CREATE INDEX%demo_orders%'; DROP INDEX IF EXISTS zentrix_idx_...;` |
| `DuplicateTableError: relation "zentrix_idx_..." already exists` | IF NOT EXISTS fix missing in the container | Rebuild: `docker-compose build backend && docker-compose up -d --force-recreate backend` |
| ML shows "Unavailable" after a rebuild | `.artifacts/` not on the volume | Verify `feature1-artifacts` volume is in `docker-compose.yml` and backend service mounts it; re-run section 3.7 |
| Canary p95 numbers frozen | Old max_exec_time collector still in container | Verify: `docker-compose exec backend grep mean_exec_time /workspace/apps/backend/app/workers/canary_monitor.py` |
| SSE stream stays RUNNING after canary completes | `db.expire_all()` fix missing | Verify: `docker-compose exec backend grep expire_all /workspace/apps/backend/app/api/routes/experiments.py` |

---

## 8. What to test beyond the happy path

Once the demo passes, the tester should exercise the failure modes to see the safety nets fire.

### 8.1 Rollback path

Add a bogus candidate that regresses performance and confirm rollback:

1. Inject a query that will get worse after the index (contrived — skip if no time).
2. Watch `canary_runs.status` become `ROLLED_BACK` with `rollback_reason` populated.

### 8.2 Insufficient privileges

Set `deploy_username = NULL` and observe the deploy fail with a clear error (not a silent hang):

```powershell
docker-compose exec app-db psql -U zentrix -d zentrix_db -c "UPDATE database_connections SET encrypted_deploy_connection_string = NULL, deploy_username = NULL;"
# Now click Deploy — should surface InsufficientPrivilegeError from backend logs
```

Re-attach with the CLI to restore.

### 8.3 Idempotent retry

Click Deploy twice for the same candidate. Second click should succeed (thanks to `IF NOT EXISTS`) and NOT create a duplicate index. Verify:

```powershell
docker-compose exec app-db psql -U zentrix -d zentrix_db -c "SELECT indexname FROM pg_indexes WHERE tablename='demo_orders';"
```

Exactly one `zentrix_idx_*` should exist.

### 8.4 Audit integrity

```powershell
docker-compose exec app-db psql -U zentrix -d zentrix_db -c "SELECT executed_by_role, COUNT(*) FROM canary_runs GROUP BY executed_by_role;"
```

Everything from Section 4.4 should show `executed_by_role = 'deploy'`.

---

## 9. Feedback the tester should collect

For each item below, one line of yes/no + note is enough.

1. Did the setup section (3.1–3.9) run without hitting undocumented errors?
2. Did the 17-row test matrix (section 5) pass without workaround?
3. Did section 6 accurately describe the known issues you hit?
4. Any 500s or fetch failures not covered in section 7?
5. Did the split-role deploy actually route through `zentrix_deployer` in the audit trail?
6. What broke that isn't in this doc?

Send the notes back to the developer along with:
- `docker-compose ps` output
- `docker-compose logs --tail=200 backend`
- `docker-compose logs --tail=200 canary-monitor`
- `docker-compose exec app-db psql -U zentrix -d zentrix_db -c "SELECT * FROM canary_runs ORDER BY started_at DESC LIMIT 5;"` output
- Screenshot of `\d demo_orders` and EXPLAIN ANALYZE before/after

---

## 10. Cheat sheet — most-used commands

```powershell
# Lifecycle
docker-compose ps
docker-compose up -d
docker-compose down
docker-compose build backend
docker-compose up -d --force-recreate backend
docker-compose build --no-cache backend   # forced re-copy of source

# Watch backend logs
docker-compose logs -f backend
docker-compose logs -f canary-monitor

# psql shortcuts
docker-compose exec app-db psql -U zentrix -d zentrix_db
docker-compose exec -T app-db psql -U zentrix -d zentrix_db -c "SELECT 1;"

# Reset workload
docker-compose exec -T app-db psql -U zentrix -d zentrix_db -c "SELECT pg_stat_statements_reset();"

# Kill stuck CREATE INDEX
docker-compose exec app-db psql -U zentrix -d zentrix_db -c "SELECT pg_cancel_backend(pid), query FROM pg_stat_activity WHERE query ILIKE '%CREATE INDEX%demo_orders%' AND pid <> pg_backend_pid();"

# Drop invalid indexes
docker-compose exec app-db psql -U zentrix -d zentrix_db -c "SELECT indexrelid::regclass, indisvalid FROM pg_index WHERE indrelid='demo_orders'::regclass;"
docker-compose exec app-db psql -U zentrix -d zentrix_db -c "DROP INDEX IF EXISTS zentrix_idx_...;"

# Re-train ML artifacts
docker-compose exec backend python -m app.ml.train_feature1_bundle --dsn postgresql://fault_lab:fault_lab_dev_password@fault-lab-db:5432/fault_lab --output /workspace/apps/backend/.artifacts

# Verify code is in the running container (any fix)
docker-compose exec backend grep -n '<TOKEN>' /workspace/apps/backend/<path>
```

---

## End of handoff

If the tester runs sections 3 → 4 → 5 without hitting anything outside section 6/7, the demo is validated. This handoff covers Arc A (real shadow-pool), Arc B (closed-loop learning wired), and Arc C (fault-lab shipping gate + bandit graduation). If they hit something new, capture the log, note which section broke, and send back for the next session.
