# Odoo cost reconnect preflight

This is an offline, library-only preparation slice. It does not add an application,
HTTP route, automatic reconnect, source registration update, or deployment.
Run these checks from the repository root with Python 3.12 or newer:

```sh
python3 -B scripts/validate-source-policy
python3 -B scripts/validate-odoo-cost-reconnect
```

The checks use synthetic metadata, credentials and provider receipts. The driver
and DNS are mocked and real socket access is blocked in the reconnect check.
They require only the Python standard library; they do not install dependencies,
read an operator's configuration or connect to DB, Superset or a model.

`runtime/api/odoo_cost_reconnect.py` exposes the internal
`inspect_cost_reconnect(config, registered, provider_target=...)` helper.
`config` must be assembled server-side from the protected existing registration.
`registered` is a server-owned joined source/mapping/dataset snapshot using DB
column names, with an immutable prior provider database identity receipt.
The helper keeps the exact host, port, database, reader, TLS, cost mapping,
company, scope and schema/module/column pins. Fresh inspection must pass the full
read-only contract. A changed DNS address after provider confirmation is rejected
before the driver connects. Existing registered-address guards remain unchanged.

`ProviderTargetBinding` is a data carrier, not authentication. Only an authenticated
provider path may create it after proving that the current target matches the
previously recorded physical database identity. A user JSON field, boolean,
matching schema or current IP observation is not that proof. Provider authentication
and immutable receipt storage are not implemented here. Existing registrations
without that receipt are rejected before connection; do not invent a receipt or
overwrite a stored address to make them pass.

The unchanged mapping/scoped-query/Superset modules are included to close existing
Python imports. Their sales, application DB and HTTP entry points are not enabled
or tested by this slice. Actual library use requires the existing server runtime
and psycopg 3; the public root project's psycopg2 dependency does not supply it.
The exact synthetic contract files are under `runtime/odoo-cost-mapping`; the
server's `TWINBI_ODOO_COST_MAPPING_DIR` must refer to that approved contract when
performing an authorized inspection. No runtime launcher or fixture initializer is
included. This slice proves neither live Odoo connectivity nor DB identity, UI
arithmetic, chart provenance, multi-turn rehearsal or a demo recording.
