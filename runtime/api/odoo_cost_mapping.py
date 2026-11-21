"""E2 onboarding and fixed, bounded product/month preview. No chat workflow."""
import json
from decimal import Decimal

import psycopg

from odoo_cost_guard import COMPANY, MAPPING, VIEW, check_cost, contract
from odoo_mapping import dataset_columns, digest
from postgres_source import SourceRejected, connect_postgresql, inspect_principal
from scoped_query import load_dataset, source_connection
from scoped_query import superset_query_client

PREVIEW_QUERY = f"""SELECT invoice_month,company,currency,product,unit,
    sum(quantity) AS quantity,sum(revenue) AS revenue,sum(cogs) AS cogs,
    sum(gross_profit) AS gross_profit,count(*) AS product_line_count
    FROM {VIEW} GROUP BY 1,2,3,4,5 ORDER BY invoice_month,product LIMIT 501"""


def inspect_cost_mapping(config, *, expected_hostaddr=None):
    spec = contract()
    hostaddr, connection = connect_postgresql(config, expected_hostaddr=expected_hostaddr)
    reasons, columns, count, size = [], [], 0, 0
    with connection:
        identity = inspect_principal(connection, config, "public")
        if identity[1] != config.database:
            raise SourceRejected("database_mismatch")
        if (config.company_id, config.company_name) != (1, COMPANY):
            reasons.append("odoo_cost_company_scope_mismatch")
        else:
            try:
                with connection.cursor() as cursor:
                    evidence = check_cost(cursor)
                columns = dataset_columns(connection, VIEW)
                count = connection.execute(f"SELECT count(*) FROM {VIEW}").fetchone()[0]
                size = evidence["relation_bytes"]
            except SourceRejected as error:
                if error.reason in {"odoo_cost_privileged_role", "odoo_cost_write_privilege", "odoo_cost_read_only_required"}:
                    raise
                reasons.append(error.reason)
            except psycopg.Error:
                reasons.append("odoo_cost_contract_unreadable")
    return {
        "status": "catalog_only" if reasons else "validated", "reasons": reasons,
        "hostaddr": hostaddr, "principal": identity[0], "mapping_id": MAPPING,
        "company_id": config.company_id, "company_name": config.company_name,
        "scope_proof": config.scope_proof, "schema_hash": spec["schema_sha256"] if not reasons else "unverified",
        "module_hash": spec["module_sha256"] if not reasons else None, "view_equivalent": not reasons,
        "visible_scope": {"companies": [1], "moves": [1], "lines": [1]} if not reasons else {},
        "estimated_rows": count, "relation_bytes": size,
        "dataset_schema_fingerprint": digest(json.dumps(columns, separators=(",", ":"))),
        "columns": columns,
    }


def cost_preview(app_connect, workspace_id, dataset_id, user_id):
    dataset = load_dataset(app_connect, workspace_id, dataset_id)
    if dataset["mapping_id"] != MAPPING or dataset["table"] != VIEW:
        raise SourceRejected("odoo_cost_dataset_required")
    with app_connect() as guard:
        if not guard.execute("SELECT pg_try_advisory_lock(hashtextextended(%s,0))",
                             (str(dataset["source_id"]),)).fetchone()[0]:
            raise SourceRejected("profile_busy")
        try:
            with source_connection(dataset) as connection:
                connection.execute("SET LOCAL statement_timeout='5s'")
                principal, read_only, temporary_bytes = connection.execute(
                    "SELECT current_user, current_setting('transaction_read_only')::boolean, "
                    "pg_size_bytes(current_setting('temp_file_limit'))"
                ).fetchone()
                if principal != dataset["principal"] or not read_only:
                    raise SourceRejected("odoo_cost_query_scope_invalid")
                if not 0 <= temporary_bytes <= 32 * 1024 * 1024:
                    raise SourceRejected("odoo_cost_temporary_budget_missing")
                connection.execute("EXPLAIN (FORMAT JSON) " + PREVIEW_QUERY).fetchone()
                rows = connection.execute(PREVIEW_QUERY).fetchall()
                lock = connection.execute("SELECT hard_lock_date FROM res_company WHERE id=1").fetchone()[0]
            if len(rows)>500:
                raise SourceRejected("odoo_cost_result_budget_exceeded")
            names = ["invoice_month","company","currency","product","unit","quantity","revenue","cogs","gross_profit","product_line_count"]
            result = [dict(zip(names,[str(v) if isinstance(v,Decimal) or i==0 else v for i,v in enumerate(row)])) for row in rows]
            if len(json.dumps(result).encode())>1024*1024:
                raise SourceRejected("odoo_cost_result_budget_exceeded")
            response = superset_query_client(user_id).chart_data({
                "datasource":{"id":dataset["superset_dataset_id"],"type":"table"},"force":True,
                "queries":[{"filters":[],"columns":names[:5],"metrics":[
                    {"expressionType":"SQL","sqlExpression":f"SUM({name})","label":name}
                    for name in names[5:9]]+[{"expressionType":"SQL","sqlExpression":"COUNT(*)","label":"product_line_count"}],
                    "row_limit":501}],"result_format":"json","result_type":"full"})
            try:
                bi_rows = response["result"][0]["data"]
                # Superset JSON encodes date columns as epoch milliseconds.
                from datetime import datetime, timezone
                for row in bi_rows:
                    if isinstance(row["invoice_month"], (int,float)):
                        row["invoice_month"] = datetime.fromtimestamp(row["invoice_month"]/1000,timezone.utc).date().isoformat()
                def normalized(items):
                    return sorted(tuple(str(row[n]) if n in names[:5] else Decimal(str(row[n])) for n in names) for row in items)
                if normalized(bi_rows) != normalized(result):
                    raise SourceRejected("principal_scope_mismatch")
            except (KeyError,ValueError,TypeError,IndexError):
                raise SourceRejected("superset_result_invalid") from None
            return {"dataset_id": dataset_id, "rows": result, "closed_through": str(lock),
                    "evidence": {"principal": dataset["principal"], "workspace_id": workspace_id,
                       "query_budget_ms":5000,"relation_max_bytes":100*1024*1024,
                       "result_max_rows":500,"result_max_bytes":1024*1024,
                       "temporary_max_bytes":32*1024*1024,"source_concurrency":1,
                       "read_only":True,"row_set_equivalent":True,"mapping_id":MAPPING,"refresh":"on request; not ERP event sync"}}
        finally:
            guard.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", (str(dataset["source_id"]),))
