"""App metadata adoption primitives; caller owns authorization and transaction.

No ERP connection, provider authentication, BI asset creation or application route.
"""
import hashlib
import json
import re

from odoo_cost_guard import COMPANY
from source_policy import source_target


def binding_digest(binding):
    return "sha256:" + hashlib.sha256(
        json.dumps(binding, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def archived_kind(dashboard_id):
    return f"cost_archived_{int(dashboard_id)}"


def registration_digest(registration):
    return binding_digest({key: value for key, value in registration.items() if key != "recent"})


def lock_cost_binding(cursor, workspace):
    cursor.execute("SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0))",
                   ("cost-baseline:" + workspace,))
    if cursor.fetchone() != (True,):
        raise RuntimeError("cost baseline operation busy")
    cursor.execute(
        "SELECT dataset_id,superset_dashboard_id,binding_json FROM workspace_dashboards "
        "WHERE workspace_id=%s AND kind='cost' FOR UPDATE", (workspace,))
    return cursor.fetchone()


def baseline_registration(cursor, dataset_id):
    cursor.execute("""SELECT d.source_id,d.workspace_id,d.schema_name,d.table_name,
        d.schema_fingerprint,d.columns_json,s.source_kind,s.status,s.host,s.hostaddr,
        s.port,s.database_name,s.username,s.query_principal,s.sslmode,
        m.mapping_id,m.mapping_status,m.company_id,m.company_name,m.scope_proof,
        m.schema_hash,m.module_hash,m.view_equivalent,m.visible_scope_json,
        d.superset_dataset_id,s.superset_database_id,
        s.created_at > clock_timestamp()-interval '5 minutes'
          AND s.created_at <= clock_timestamp()
        FROM source_datasets d JOIN data_sources s ON s.source_id=d.source_id
        JOIN odoo_mappings m ON m.source_id=s.source_id AND m.dataset_id=d.dataset_id
        WHERE d.dataset_id=%s AND d.workspace_id=s.workspace_id
          AND m.workspace_id=d.workspace_id FOR SHARE OF d,s,m""", (dataset_id,))
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError("cost baseline registration missing")
    return dict(zip(("source_id", "workspace", "schema", "table", "fingerprint", "columns",
        "source_kind", "status", "host", "hostaddr", "port", "database", "username", "principal", "sslmode",
        "mapping", "mapping_status", "company_id", "company_name", "scope", "schema_hash", "module_hash",
        "view_equivalent", "visible_scope", "bi_dataset_id", "bi_database_id", "recent"), row, strict=True))


def adoption_preflight(cursor, workspace, current, dataset_id, adopt_from, expected_binding, expected_source, decision_ref):
    """Operator-only new basis. A fresh normal registration is not same-DB proof."""
    if (current is None or str(current[0]) != adopt_from or dataset_id == adopt_from
            or binding_digest(current[2]) != expected_binding
            or not isinstance(decision_ref, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", decision_ref) is None):
        raise RuntimeError("explicit cost baseline approval or binding changed")
    old, new = baseline_registration(cursor, adopt_from), baseline_registration(cursor, dataset_id)
    if registration_digest(old) != expected_source:
        raise RuntimeError("cost baseline original registration changed")
    if (str(old["source_id"]) == str(new["source_id"])
            or old["bi_dataset_id"] == new["bi_dataset_id"]
            or old["bi_database_id"] == new["bi_database_id"]
            or current[2].get("source_id") != str(old["source_id"])
            or current[2].get("dataset_id") != adopt_from
            or current[2].get("schema_fingerprint") != old["fingerprint"]
            or new["recent"] is not True):
        raise RuntimeError("cost baseline requires distinct fresh registered source and dataset")
    pins = ("workspace", "schema", "table", "fingerprint", "columns", "source_kind", "status", "host",
            "port", "database", "username", "principal", "sslmode", "mapping", "mapping_status",
            "company_id", "company_name", "scope", "schema_hash", "module_hash", "view_equivalent", "visible_scope")
    if any(old[key] is None or old[key] != new[key] for key in pins):
        raise RuntimeError("cost baseline reader or contract pins changed")
    if (new["workspace"] != workspace or new["schema"] != "public"
            or new["table"] != "twinbi_invoiced_product_cost_v1"
            or new["mapping"] != "invoiced_product_cost_v1" or new["source_kind"] != "odoo19"
            or new["status"] != "ready" or new["mapping_status"] != "validated"
            or new["username"] != new["principal"] or not new["username"]
            or new["view_equivalent"] is not True or not new["columns"]
            or type(new["company_id"]) is not int or new["company_id"] != 1
            or new["company_name"] != COMPANY or new["scope"] != "database_rls"
            or new["visible_scope"] != {"companies": [1], "moves": [1], "lines": [1]}
            or any(not isinstance(new[key], str) or re.fullmatch(r"sha256:[0-9a-f]{64}", new[key]) is None
                   for key in ("fingerprint", "schema_hash", "module_hash"))):
        raise RuntimeError("verified exact cost baseline required")
    target, ca = source_target(new["host"], new["port"], new["sslmode"])
    if target["hostaddr"] != new["hostaddr"]:
        raise RuntimeError("cost baseline source address changed")
    return {"kind": "new_target_baseline", "identity_continuity": "unproven",
            "decision_ref": decision_ref, "previous_source_id": str(old["source_id"]),
            "previous_dataset_id": adopt_from, "previous_dashboard_id": current[1],
            "previous_binding_digest": expected_binding, "previous_registration_digest": expected_source,
            "target": target, "ca": ca}


def archive_current(cursor, workspace, current):
    cursor.execute("""UPDATE workspace_dashboards SET kind=%s
        WHERE workspace_id=%s AND kind='cost' AND dataset_id=%s
          AND superset_dashboard_id=%s AND binding_json=%s::jsonb""",
        (archived_kind(current[1]), workspace, str(current[0]), current[1], json.dumps(current[2])))
    if cursor.rowcount != 1:
        raise RuntimeError("cost baseline binding race")


def rollback_baseline(cursor, workspace, dataset_id, restore_dataset, expected_binding):
    """Caller commits one app transaction; retain both dashboard rows and all history."""
    current = lock_cost_binding(cursor, workspace)
    if (not current or str(current[0]) != dataset_id or binding_digest(current[2]) != expected_binding
            or current[2].get("baseline", {}).get("previous_dataset_id") != restore_dataset):
        raise RuntimeError("cost baseline rollback binding changed")
    previous = current[2]["baseline"]
    cursor.execute("""SELECT dataset_id,superset_dashboard_id,binding_json FROM workspace_dashboards
        WHERE workspace_id=%s AND kind=%s AND dataset_id=%s FOR UPDATE""",
        (workspace, archived_kind(previous["previous_dashboard_id"]), restore_dataset))
    restored = cursor.fetchone()
    if (not restored or restored[1] != previous["previous_dashboard_id"]
            or binding_digest(restored[2]) != previous["previous_binding_digest"]
            or registration_digest(baseline_registration(cursor, restore_dataset))
                != previous["previous_registration_digest"]):
        raise RuntimeError("cost baseline rollback original binding changed")
    archive_current(cursor, workspace, current)
    cursor.execute("""UPDATE workspace_dashboards SET kind='cost'
        WHERE workspace_id=%s AND kind=%s AND dataset_id=%s AND superset_dashboard_id=%s""",
        (workspace, archived_kind(restored[1]), restore_dataset, restored[1]))
    if cursor.rowcount != 1:
        raise RuntimeError("cost baseline rollback race")
    return {"status": "configuration_restored_live_read_unverified", "dataset_id": restore_dataset,
            "dashboard_id": restored[1], "archived_dashboard_id": current[1]}
