# Draft rollback-only PostgreSQL probe

This tests/docs-only slice depends on unmerged [PR #18](https://github.com/simonjisu/TwinBI/pull/18),
head `3d5c696a316b2c0255de4e9a50ccc40a47d6c2bc`. Public `v2` at
`5d785020c9aefb682804f18710beb8eaed517c15` does not contain its component.
This PR does not copy that module, merge its branch or publish the absent app.
Keep the new PR Draft until that dependency and independent real-PG evidence exist.

Run the default checks with only Python's standard library:

```sh
python3 -B scripts/validate-baseline-pg-transactions
```

These seven checks use test doubles for session/control flow, pins, trigger refusal,
rollback failure, missing dependency/module-cache pollution, source checksum and
CLI error redaction. Unknown options and invalid typed values produce fixed errors.
They do not import or test PR #18's helpers, connect to PostgreSQL or validate
actual lock semantics. Output explicitly records `public_component_tested=false`
and `real_pg_executed=false`.

Real mode requires an operator's explicit opt-in and all target pins:

```sh
python3 -B scripts/validate-baseline-pg-transactions --real --rollback-only \
  --database EXPECTED_DATABASE --db-user EXPECTED_USER --workspace WORKSPACE \
  --source-id SOURCE_UUID --dataset-id DATASET_UUID --dashboard-id DASHBOARD_ID \
  --expected-row sha256:CANONICAL_FULL_ROW_HASH
```

The script loads `runtime/api/cost_baseline.py` from its reviewed path only after
checking SHA256 `5d3c4761c7de2178e6933531c4585be68c85d98ff6e47aff22a7b95a517b84bb`.
It delegates `binding_digest`, `archived_kind`, `lock_cost_binding` and
`archive_current`; their implementations are not copied into the test. A missing
or changed component fails before importing the real driver or reading a DSN.
There is no module-cache fallback, automatic download or helper injection. Updating
the component version requires an explicit reviewed checksum update.

Real mode also requires externally provided psycopg 3 and an existing authorized
App database/schema. It does not install packages or create a database/schema/role.
Connection settings come only from protected process environment: either
`TWINBI_BASELINE_PROBE_DSN` or existing `DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`.
Do not put credentials in command arguments, Git, logs or the result. The supplied
database/user names are verified against both actual sessions before any archive.
The full-row pin uses PR #18's `binding_digest` on the active row JSON; it differs
from hashing PostgreSQL's JSON-text representation directly.

The probe uses two independent transactions and bounded statement/lock waits.
It checks advisory contention, row `NOWAIT` SQLSTATE `55P03`, and reacquisition
after rollback. Exact source/dataset/dashboard/full-row pins are mandatory.
Before its single uncommitted archive, a table lock prevents racing trigger/rule
DDL, and any noninternal trigger, rewrite rule or nonordinary table blocks writing.
An exception is injected immediately after archive, before any new insert. Session B
must still see the committed active row. The original full row must be restored.
Every connection is rolled back and closed on success or failure; commit/delete
paths are absent. Cleanup failure is uncertain, never PASS. Exit codes are 0 for
PASS, 2 for BLOCKED, and 1 for failed/uncertain real checks.

Five read-only SQL cases freeze a fixture clock and check the reviewed component's
`created_at` five-minute expression, including the strict boundary and a future
timestamp. Decision/query timestamps do not refresh registration age. No stored
registration timestamp is changed. These are expression cases, not end-to-end
source-registration coverage.

Offline self-checks are not real-PG evidence. Record exact script/component checksums
and separate real-run results before claiming live coverage. The probe temporarily
locks and updates an existing App metadata row inside a transaction, so an operator
must coordinate it with active work. It never connects to ERP, invokes a model,
changes source/role/security settings, deploys services or restarts Docker.
It does not cover the absent session/relay archived-kind guard, workflow approval,
registration IP forwarding, Superset/App commit-ACK reconciliation, a retry
orchestrator or the complete adoption/UI flow. Those #18 integration gaps remain
open; passing this probe must not be presented as full adoption validation.
