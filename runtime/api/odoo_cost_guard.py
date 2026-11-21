"""Exact E2 read contract, shared by psycopg API and Superset DBAPI cursors."""
import hashlib
import json
import os
from pathlib import Path

from source_policy import SourceRejected

MAPPING = "invoiced_product_cost_v1"
VIEW = "twinbi_invoiced_product_cost_v1"
APPLICATION = "twinbi-cost-v1:1"
COMPANY = "한빛상품"
MAX_BYTES = 100 * 1024 * 1024


def contract():
    root = Path(os.environ["TWINBI_ODOO_COST_MAPPING_DIR"])
    spec = json.loads((root / "mapping.json").read_text())
    spec["projection_query"] = (root / "projection.sql").read_text().strip().rstrip(";")
    spec["invalid_period_query"] = (root / "invalid-period.sql").read_text()
    return spec


def rows(cursor, query, parameters=()):
    cursor.execute(query, parameters)
    return cursor.fetchall()


def fingerprint(value):
    return "sha256:" + hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()


def require(condition, reason):
    if not condition:
        raise SourceRejected("odoo_cost_" + reason)


def check_cost(cursor):
    """No writes, user SQL, new connections, or ORM. Runs before every live read."""
    spec = contract()
    tables = list(spec["columns"])
    require(rows(cursor, "SELECT current_setting('transaction_read_only'), current_setting('row_security')")[0] == ("on", "on"), "read_only_required")
    require(not rows(cursor, """SELECT 1 FROM pg_roles WHERE pg_has_role(current_user,oid,'MEMBER')
        AND (rolsuper OR rolbypassrls OR rolcreatedb OR rolcreaterole OR rolreplication)"""), "privileged_role")
    require(not rows(cursor, """SELECT 1 FROM pg_database WHERE datname=current_database() AND
        (pg_has_role(current_user,datdba,'MEMBER') OR has_database_privilege(current_user,oid,'CREATE')
         OR has_database_privilege(current_user,oid,'TEMP')) UNION ALL
        SELECT 1 FROM pg_namespace WHERE nspname='public' AND
        (pg_has_role(current_user,nspowner,'MEMBER') OR has_schema_privilege(current_user,oid,'CREATE'))"""), "write_privilege")
    relation = rows(cursor, """SELECT c.relname,c.relkind,c.relrowsecurity,c.reloptions,
        pg_has_role(current_user,c.relowner,'MEMBER'),pg_total_relation_size(c.oid),
        (has_table_privilege(current_user,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
          OR has_any_column_privilege(current_user,c.oid,'INSERT,UPDATE,REFERENCES'))
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relname=ANY(%s) ORDER BY c.relname""", (tables + [VIEW],))
    require({r[0] for r in relation} == set(tables + [VIEW]), "relation_missing")
    require(not any(r[4] or r[6] for r in relation), "write_privilege")
    require(sum(r[5] for r in relation) <= MAX_BYTES, "relation_budget_exceeded")
    require(all(r[1] == ("v" if r[0] == VIEW else "r") for r in relation), "relation_kind")
    view = next(r for r in relation if r[0] == VIEW)
    require({"security_invoker=true", "security_barrier=true"} <= set(view[3] or []), "view_security")
    for table in spec["rls"]:
        require(next(r for r in relation if r[0] == table)[2], "rls_missing")
    policies = rows(cursor, """SELECT c.relname, p.polcmd, p.polpermissive,
        pg_get_expr(p.polqual,p.polrelid), pg_get_expr(p.polwithcheck,p.polrelid)
        FROM pg_policy p JOIN pg_class c ON c.oid=p.polrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public'
        AND c.relname=ANY(%s) AND (0=ANY(p.polroles) OR EXISTS
        (SELECT 1 FROM pg_roles r WHERE r.oid=ANY(p.polroles) AND pg_has_role(current_user,r.oid,'MEMBER')))
        ORDER BY c.relname,p.polname""", (list(spec["rls"]),))
    require([list(r) for r in policies] == spec["policies"], "rls_policy_changed")
    columns = rows(cursor, """SELECT c.relname,a.attname FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_attribute a ON a.attrelid=c.oid
        WHERE n.nspname='public' AND c.relname=ANY(%s) AND a.attnum>0 AND NOT a.attisdropped
        AND has_column_privilege(current_user,c.oid,a.attnum,'SELECT') ORDER BY 1,2""", (tables,))
    require(set(columns) == {(t,c) for t,cs in spec["columns"].items() for c in cs}, "column_policy_changed")
    require(not rows(cursor, """SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_attribute a ON a.attrelid=c.oid WHERE n.nspname='public'
        AND c.relkind IN ('r','p','v','m','f') AND NOT c.relname=ANY(%s)
        AND a.attnum>0 AND NOT a.attisdropped AND has_column_privilege(current_user,c.oid,a.attnum,'SELECT') LIMIT 1""", (tables + [VIEW],)), "unapproved_relation_access")
    modules = rows(cursor, "SELECT name,latest_version FROM ir_module_module WHERE state='installed' ORDER BY name")
    require(fingerprint(modules) == spec["module_sha256"], "module_fingerprint_changed")
    schema = rows(cursor, """SELECT c.relname,a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_attribute a ON a.attrelid=c.oid
        WHERE n.nspname='public' AND c.relname=ANY(%s) AND a.attnum>0 AND NOT a.attisdropped ORDER BY c.relname,a.attnum""", (tables + [VIEW],))
    require(fingerprint(schema) == spec["schema_sha256"], "schema_fingerprint_changed")
    definition = rows(cursor, "SELECT pg_get_viewdef(%s::regclass,true)", ("public." + VIEW,))[0][0]
    require(fingerprint(definition) == spec["view_sha256"], "view_definition_changed")
    company = rows(cursor, """SELECT c.id,c.name,r.name,c.cost_method,c.inventory_valuation,c.inventory_period,
        c.chart_template,c.hard_lock_date,c.expense_account_id,c.account_stock_valuation_id
        FROM res_company c JOIN res_currency r ON r.id=c.currency_id ORDER BY c.id""")
    require(len(company) == 1 and company[0][:7] == (1,COMPANY,"KRW","fifo","real_time","manual","kr"), "company_policy_changed")
    lock = company[0][7]
    require(str(lock) in {"2026-02-28","2026-03-31"}, "unsupported_fixture_period")
    require(company[0][8:] == tuple(spec["accounts"]), "account_policy_changed")
    require(rows(cursor,"SELECT id,code_store->>'1',account_type FROM account_account WHERE id=ANY(%s) ORDER BY id",
                 (spec["accounts"],)) == [tuple(r) for r in spec["account_policy"]], "account_policy_changed")
    require(rows(cursor,"SELECT relative_factor,relative_uom_id FROM uom_uom WHERE id=1") == [(1,None)], "unit_policy_changed")
    defaults = rows(cursor,"""SELECT f.model,f.name,d.company_id,d.user_id,d.json_value FROM ir_default d
        JOIN ir_model_fields f ON f.id=d.field_id ORDER BY f.model,f.name,d.company_id,d.user_id,d.json_value""")
    require([list(r) for r in defaults] == spec["defaults"], "default_policy_changed")
    require(not rows(cursor, spec["invalid_period_query"]), "period_incomplete")
    query = spec["projection_query"]
    # Exact definition plus independent set comparison: an empty/drifted view is not zero cost.
    require(not rows(cursor, f"SELECT 1 FROM (({query}) EXCEPT ALL (SELECT * FROM {VIEW})) d LIMIT 1"), "view_not_equivalent")
    require(not rows(cursor, f"SELECT 1 FROM ((SELECT * FROM {VIEW}) EXCEPT ALL ({query})) d LIMIT 1"), "view_not_equivalent")
    return {"closed_through": str(lock), "module_hash": spec["module_sha256"],
            "schema_hash": spec["schema_sha256"], "relation_bytes": sum(r[5] for r in relation)}
