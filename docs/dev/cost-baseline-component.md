# Cost baseline metadata component

This is a library-only component for review. It does not deliver an application,
source registration, dashboard bootstrap, adoption CLI, deployment or a live demo.
The eight helpers expose admission predicates and transaction-scoped app-metadata
operations. The caller supplies an authorized PostgreSQL app cursor; no connector
or service is created. Importing this module needs only Python's standard library
and the existing public source-policy/cost-guard modules.

Run the offline checks from the repository root with Python 3.12 or newer:

```sh
python3 -B scripts/validate-cost-baseline-component
python3 -B scripts/validate-odoo-cost-reconnect
python3 -B scripts/validate-source-policy
```

The component suite runs eight tests using synthetic registrations and an in-memory
SQLite FK/unique oracle. DNS is mocked and socket access is blocked. PostgreSQL
lock calls are modeled; their real concurrency behavior is not validated by SQLite.
No operator configuration, secret, DB, Superset, API, model or UI is accessed.

## Component contract

A new baseline uses distinct source/dataset/BI IDs and explicitly records that
physical continuity with the old target is unproven. `adoption_preflight` consumes
server-owned registration snapshots; it does not perform a fresh ERP read or
authenticate an approval. The original binding and registration digests, operator
decision reference, fresh normal registration, exact reader/endpoint/security and
mapping pins, current address policy and confirmed address must match. The caller
must establish the approval and actual read-only validation before using it.
No provider identity receipt is invented, and this is separate from the existing
strict same-physical-database preflight.

`lock_cost_binding` requests a workspace advisory transaction lock and row lock.
`baseline_registration` takes shared locks on app registration metadata. The caller
must keep one app transaction open through compare-and-swap archival and insertion
of the new active binding. `archive_current` changes only the old row's kind;
the dashboard ID, binding, FK-backed evidence and run records stay unchanged.
It does not create the new active row or any BI asset. `rollback_baseline` checks
the recorded old binding/registration digests, archives the current row and restores
the previous kind in that same caller-owned transaction. It never deletes either
history or changes an ERP/source address. Its result explicitly leaves live read
verification pending; the original address guard must continue rejecting DNS drift.

## Integration not included

The following four tests from the local full-app suite cannot run in this public
tree and have **not** been silently counted as passing:

- `test_archived_kinds_are_rejected_by_all_three_read_entry_points`: session,
  ownership and relay archived-kind rejection requires absent app/analysis code.
- `test_existing_workflow_approval_cannot_inherit_new_dataset`: workflow approval
  separation requires the absent workflow runtime.
- `test_provision_expected_address_is_server_only_and_forwarded`: request-schema
  and registration IP-forwarding integration requires the absent app code.
- `test_operator_registration_forwards_confirmed_address_before_control_writes`:
  operator registration ordering also requires that app code.

Those integrations are required before exposing this component as an operational
adoption path. Also absent are the app schema/migrations and transaction caller,
normal source/BI registration, fresh provider/read-only validation capture,
Superset asset staging, archived-source deletion protection, cross-system partial
failure reconciliation, CLI rollback handling, real PostgreSQL commit/lock tests,
workflow reapproval and model/UI/demo evidence. If BI commit succeeds but app commit
fails or is uncertain, a future caller must reconcile both sides and must not claim
success or delete historical objects. This component alone does not enforce the
complete integration boundary and should remain a Draft review slice.
