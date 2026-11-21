import hashlib
import json
import os
from pathlib import Path

from psycopg import sql

from postgres_source import connect_postgresql, inspect_principal, SourceRejected


SCHEMA = "public"
VIEW = "twinbi_monthly_net_invoiced_revenue_v1"
RLS_TABLES = {"account_move", "account_move_line", "res_company"}

MAPPING_QUERY = """
WITH scoped AS (
    SELECT
        date_trunc('month', COALESCE(move.invoice_date, move.date))::date
            AS invoice_month,
        move.id AS move_id,
        move.move_type,
        -line.balance AS revenue
    FROM account_move AS move
    JOIN account_move_line AS line ON line.move_id = move.id
    JOIN res_company AS company ON company.id = move.company_id
    JOIN res_currency AS currency ON currency.id = company.currency_id
    WHERE company.id = %s
      AND company.name = %s
      AND currency.name = 'KRW'
      AND move.currency_id = company.currency_id
      AND line.company_id = move.company_id
      AND line.currency_id = company.currency_id
      AND move.state = 'posted'
      AND move.move_type IN ('out_invoice', 'out_refund')
      AND line.display_type = 'product'
), monthly AS (
    SELECT
        invoice_month,
        sum(revenue) AS net_revenue,
        sum(revenue) FILTER (WHERE move_type = 'out_invoice') AS invoice_revenue,
        sum(revenue) FILTER (WHERE move_type = 'out_refund') AS refund_revenue,
        count(DISTINCT move_id) AS document_count,
        count(*) AS product_line_count
    FROM scoped
    GROUP BY invoice_month
)
SELECT
    invoice_month,
    %s::text AS company,
    'KRW'::text AS currency,
    net_revenue,
    invoice_revenue,
    COALESCE(refund_revenue, 0) AS refund_revenue,
    document_count,
    product_line_count
FROM monthly
ORDER BY invoice_month
"""


def digest(text):
    return f"sha256:{hashlib.sha256(text.encode()).hexdigest()}"


def load_contract():
    root = Path(os.environ["TWINBI_ODOO_MAPPING_DIR"])
    manifest = json.loads((root / "manifest.json").read_text())
    policy = json.loads((root / manifest["mapping"]["column_policy"]).read_text())
    return manifest, policy, (root / "required-schema.sql").read_text()


def relation_metadata(connection, names):
    return {
        row[0]: {
            "kind": row[1],
            "owned": row[2],
            "rls": row[3],
            "options": set(row[4] or []),
        }
        for row in connection.execute(
            """
            SELECT relation.relname, relation.relkind,
                   pg_get_userbyid(relation.relowner) = current_user,
                   relation.relrowsecurity, relation.reloptions
            FROM pg_class AS relation
            JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = %s AND relation.relname = ANY(%s)
            """,
            (SCHEMA, list(names)),
        ).fetchall()
    }


def accessible_columns(connection, tables):
    return {
        (row[0], row[1])
        for row in connection.execute(
            """
            SELECT relation.relname, attribute.attname
            FROM pg_class AS relation
            JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
            JOIN pg_attribute AS attribute ON attribute.attrelid = relation.oid
            WHERE namespace.nspname = %s
              AND relation.relname = ANY(%s)
              AND attribute.attnum > 0
              AND NOT attribute.attisdropped
              AND has_column_privilege(
                    current_user, relation.oid, attribute.attnum, 'SELECT'
                  )
            """,
            (SCHEMA, list(tables)),
        ).fetchall()
    }


def assert_no_write_privilege(connection, relations):
    for relation in relations:
        qualified = sql.Identifier(SCHEMA, relation).as_string(connection)
        privileges = connection.execute(
            """
            SELECT has_table_privilege(current_user, %s, 'INSERT'),
                   has_table_privilege(current_user, %s, 'UPDATE'),
                   has_table_privilege(current_user, %s, 'DELETE'),
                   has_table_privilege(current_user, %s, 'TRUNCATE'),
                   has_table_privilege(current_user, %s, 'REFERENCES'),
                   has_table_privilege(current_user, %s, 'TRIGGER')
            """,
            (qualified,) * 6,
        ).fetchone()
        if any(privileges):
            raise SourceRejected("table_write_capability")


def dataset_columns(connection, view=VIEW):
    rows = connection.execute(
        """
        SELECT column_name, data_type, is_nullable, ordinal_position
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
        """,
        (SCHEMA, view),
    ).fetchall()
    return [
        {
            "name": row[0],
            "type": row[1],
            "nullable": row[2] == "YES",
            "position": row[3],
        }
        for row in rows
    ]


def inspect_odoo_mapping(config):
    manifest, policy, schema_query = load_contract()
    expected_columns = {
        (table, column)
        for table, columns in {
            **policy["allowed_source_columns"],
            **policy["allowed_metadata_columns"],
        }.items()
        for column in columns
    }
    base_tables = set(policy["allowed_source_columns"])
    metadata_tables = set(policy["allowed_metadata_columns"])
    required_relations = base_tables | metadata_tables | {VIEW}

    hostaddr, connection = connect_postgresql(config)
    try:
        with connection:
            identity = inspect_principal(connection, config, SCHEMA)
            relations = relation_metadata(connection, required_relations)
            present = set(relations)
            assert_no_write_privilege(connection, present)

            reasons = []
            if required_relations - present:
                reasons.append("required_relation_missing")
            if any(relation["owned"] for relation in relations.values()):
                raise SourceRejected("relation_owned")

            module_hash = None
            if metadata_tables <= present:
                module_lines = [
                    f"{name}|{version}"
                    for name, version in connection.execute(
                        """
                        SELECT name, latest_version
                        FROM ir_module_module
                        WHERE state = 'installed'
                        ORDER BY name
                        """
                    ).fetchall()
                ]
                module_hash = digest("\n".join(module_lines) + "\n")
            if module_hash != manifest["mapping"]["module_sha256"]:
                reasons.append("module_fingerprint_mismatch")

            schema_lines = [row[0] for row in connection.execute(schema_query)]
            schema_hash = digest("\n".join(schema_lines) + "\n")
            if schema_hash != manifest["mapping"]["schema_sha256"]:
                reasons.append("schema_fingerprint_mismatch")

            selected_columns = accessible_columns(
                connection,
                base_tables | metadata_tables,
            )
            if selected_columns != expected_columns:
                reasons.append("column_policy_mismatch")

            rls_ok = (
                RLS_TABLES <= present
                and all(relations[table]["rls"] for table in RLS_TABLES)
                and connection.execute(
                    "SELECT current_setting('row_security')"
                ).fetchone()[0]
                == "on"
            )
            if not rls_ok:
                reasons.append("rls_scope_missing")

            company = None
            visible_scope = {"companies": [], "moves": [], "lines": []}
            if base_tables <= present and expected_columns <= selected_columns:
                company = connection.execute(
                    """
                    SELECT company.id, company.name, currency.name
                    FROM res_company AS company
                    JOIN res_currency AS currency ON currency.id = company.currency_id
                    WHERE company.id = %s AND company.name = %s
                    """,
                    (config.company_id, config.company_name),
                ).fetchone()
                visible_scope = {
                    "companies": [
                        row[0]
                        for row in connection.execute(
                            "SELECT id FROM res_company ORDER BY id"
                        ).fetchall()
                    ],
                    "moves": [
                        row[0]
                        for row in connection.execute(
                            "SELECT DISTINCT company_id FROM account_move ORDER BY company_id"
                        ).fetchall()
                    ],
                    "lines": [
                        row[0]
                        for row in connection.execute(
                            "SELECT DISTINCT company_id FROM account_move_line ORDER BY company_id"
                        ).fetchall()
                    ],
                }
            expected_scope = {config.company_id}
            if (
                company != (config.company_id, config.company_name, "KRW")
                or set(visible_scope["companies"]) != expected_scope
                or not set(visible_scope["moves"]) <= expected_scope
                or not set(visible_scope["lines"]) <= expected_scope
            ):
                reasons.append("company_scope_mismatch")

            columns = dataset_columns(connection) if VIEW in present else []
            view = relations.get(VIEW, {})
            view_ok = (
                view.get("kind") == "v"
                and not view.get("owned", True)
                and {"security_barrier=true", "security_invoker=true"}
                <= view.get("options", set())
                and [column["name"] for column in columns]
                == policy["allowed_output_columns"]
                and [[column["name"], column["type"]] for column in columns]
                == policy["allowed_output_schema"]
            )
            view_rows = []
            if view_ok and company is not None:
                view_rows = connection.execute(
                    sql.SQL("SELECT * FROM {}.{} ORDER BY invoice_month").format(
                        sql.Identifier(SCHEMA),
                        sql.Identifier(VIEW),
                    )
                ).fetchall()
                direct_rows = connection.execute(
                    MAPPING_QUERY,
                    (config.company_id, config.company_name, config.company_name),
                ).fetchall()
                view_ok = view_rows == direct_rows
            if not view_ok:
                reasons.append("view_contract_mismatch")

            relation_bytes = connection.execute(
                """
                SELECT COALESCE(sum(pg_total_relation_size(relation.oid)), 0)
                FROM pg_class AS relation
                JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                WHERE namespace.nspname = %s
                  AND relation.relname = ANY(%s)
                """,
                (SCHEMA, list(base_tables)),
            ).fetchone()[0]
    finally:
        connection.close()

    encoded_columns = json.dumps(columns, separators=(",", ":"))
    return {
        "status": "validated" if not reasons else "catalog_only",
        "reasons": reasons,
        "hostaddr": hostaddr,
        "principal": identity[0],
        "mapping_id": manifest["mapping"]["id"],
        "company_id": config.company_id,
        "company_name": config.company_name,
        "scope_proof": config.scope_proof,
        "schema_hash": schema_hash,
        "module_hash": module_hash,
        "view_equivalent": view_ok,
        "visible_scope": visible_scope,
        "estimated_rows": len(view_rows),
        "relation_bytes": relation_bytes,
        "dataset_schema_fingerprint": digest(encoded_columns),
        "columns": columns,
    }
