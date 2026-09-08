# Connection privileges (Model B split-role)

Zentrix uses **two Postgres roles per monitored database**:

- **Monitoring role** — read-only, used continuously by the telemetry
  collector and diagnosis pipeline. Never writes to the customer DB.
- **Deploy role** — elevated, used ONLY when applying an approved canary
  DDL (CREATE INDEX / ANALYZE / VACUUM). Invoked per-deploy and closed
  immediately after.

Splitting the roles keeps the always-on connection safe (a compromised
monitoring credential cannot alter the schema) and gives the customer's
DBA an auditable, revocable deploy identity.

## Provision the roles

```sql
-- 1) Monitoring role (read-only)
CREATE ROLE zentrix_monitor WITH LOGIN PASSWORD '<monitoring-password>';
GRANT USAGE ON SCHEMA public TO zentrix_monitor;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO zentrix_monitor;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO zentrix_monitor;
GRANT pg_read_all_stats TO zentrix_monitor;  -- for pg_stat_statements

-- 2) Deploy role (schema changes only)
CREATE ROLE zentrix_deployer WITH LOGIN PASSWORD '<deploy-password>';
GRANT USAGE, CREATE ON SCHEMA public TO zentrix_deployer;

-- Deploy role must OWN the tables it will index. Two ways:
--   (a) Transfer ownership of specific tables (surgical, recommended):
ALTER TABLE demo_orders OWNER TO zentrix_deployer;
--   (b) Or make deployer a member of the current owner role:
-- GRANT <current_table_owner> TO zentrix_deployer;
```

## Attach the deploy credentials to an existing connection

The connection already stores the monitoring credential. Attach the
elevated deploy credential once:

```bash
docker-compose exec backend python -m app.cli.set_deploy_credentials \
  --connection-id <uuid-from-database_connections.id> \
  --deploy-username zentrix_deployer \
  --deploy-password '<deploy-password>' \
  --host app-db \
  --port 5432 \
  --database zentrix_db \
  --sslmode disable
```

Verify:

```sql
SELECT name, username, deploy_username IS NOT NULL AS has_deploy_creds
FROM database_connections;
```

## Behavior when deploy credentials are NOT set

- Monitoring continues to work (pg_stat_statements, EXPLAIN, table stats).
- Diagnosis, recommendations, and simulation continue to work.
- **Canary deploy will fail** with `InsufficientPrivilegeError: must be
  owner of table <t>` — the monitoring role is intentionally not
  privileged to change schema.
- The `canary_runs.executed_by_role` audit column will record the role
  that ran (or attempted to run) the DDL: `"deploy"` for the split-role
  path, `"monitoring"` for the fallback.

## Rotating the deploy credential

Rerun `python -m app.cli.set_deploy_credentials` with the new password.
Existing monitoring pool is unaffected.

## Auditing

Every canary run persists which role executed the DDL in
`canary_runs.executed_by_role`. For SOC2 / customer compliance you can
export the audit trail with:

```sql
SELECT cr.id, cr.experiment_id, cr.executed_by_role, cr.canary_sql_applied,
       cr.status, cr.started_at, cr.completed_at
FROM canary_runs cr
ORDER BY cr.started_at DESC;
```
