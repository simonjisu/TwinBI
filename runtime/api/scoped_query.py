import os
from uuid import UUID

import psycopg
from psycopg import sql

from postgres_source import SourceRejected
from source_policy import source_target
from odoo_cost_guard import MAPPING as COST_MAPPING, check_cost
from superset_client import SupersetClient


def load_dataset(app_connect, workspace_id, dataset_id):
    try:
        dataset_id = str(UUID(dataset_id))
    except ValueError:
        raise SourceRejected("dataset_not_found") from None
    with app_connect() as connection:
        row = connection.execute(
            """
            SELECT dataset.dataset_id, dataset.source_id, dataset.schema_name,
                   dataset.table_name, dataset.superset_dataset_id,
                   dataset.relation_estimated_rows, dataset.relation_bytes,
                   dataset.schema_fingerprint, dataset.columns_json,
                   source.host, source.hostaddr, source.port,
                   source.database_name, source.username,
                   pgp_sym_decrypt(source.encrypted_password, %s),
                   source.sslmode, source.query_principal, mapping.mapping_id
            FROM source_datasets AS dataset
            JOIN data_sources AS source ON source.source_id = dataset.source_id
            LEFT JOIN odoo_mappings AS mapping ON mapping.source_id=source.source_id
            WHERE dataset.dataset_id = %s AND dataset.workspace_id = %s
            """,
            (os.environ["TWINBI_SOURCE_SECRET"], dataset_id, workspace_id),
        ).fetchone()
    if row is None:
        raise SourceRejected("dataset_not_found")
    keys = (
        "dataset_id",
        "source_id",
        "schema",
        "table",
        "superset_dataset_id",
        "estimated_rows",
        "relation_bytes",
        "schema_fingerprint",
        "columns",
        "host",
        "hostaddr",
        "port",
        "database",
        "username",
        "password",
        "sslmode",
        "principal",
        "mapping_id",
    )
    return dict(zip(keys, row, strict=True))


def source_connection(dataset):
    target, _ = source_target(dataset["host"], dataset["port"], dataset["sslmode"])
    if target["hostaddr"] != str(dataset["hostaddr"]):
        raise SourceRejected("source_address_changed")
    try:
        connection = psycopg.connect(
            host=dataset["host"],
            port=dataset["port"],
            dbname=dataset["database"],
            user=dataset["username"],
            password=dataset["password"],
            **target,
            connect_timeout=5,
        )
        if dataset.get("mapping_id") == COST_MAPPING:
            try:
                connection.execute("SET TRANSACTION READ ONLY")
                connection.execute("SET LOCAL statement_timeout='5s'")
                with connection.cursor() as cursor:
                    check_cost(cursor)
            except Exception:
                connection.close()
                raise
        return connection
    except psycopg.Error:
        raise SourceRejected("query_connection_failed") from None


def direct_count(dataset):
    connection = source_connection(dataset)
    try:
        with connection:
            connection.execute("SET TRANSACTION READ ONLY")
            connection.execute("SET LOCAL statement_timeout = '3s'")
            principal = connection.execute("SELECT current_user").fetchone()[0]
            count = connection.execute(
                sql.SQL("SELECT count(*) FROM {}.{}").format(
                    sql.Identifier(dataset["schema"]),
                    sql.Identifier(dataset["table"]),
                )
            ).fetchone()[0]
    except psycopg.errors.QueryCanceled:
        raise SourceRejected("query_budget_exceeded") from None
    finally:
        connection.close()
    return principal, count


def superset_query_client(user_id):
    query_users = {
        "user_alpha": "twinbi_ws_alpha_query",
        "user_beta": "twinbi_ws_beta_query",
    }
    username = query_users.get(user_id)
    if username is None:
        raise SourceRejected("workspace_role_missing")
    return SupersetClient(
        os.environ["SUPERSET_INTERNAL_URL"],
        username=username,
        password=os.environ["SUPERSET_QUERY_PASSWORD"],
    ).authenticate()


def superset_count(dataset, user_id):
    result = superset_query_client(user_id).chart_data(
        {
            "datasource": {
                "id": dataset["superset_dataset_id"],
                "type": "table",
            },
            "force": True,
            "queries": [
                {
                    "filters": [],
                    "columns": [],
                    "metrics": [
                        {
                            "expressionType": "SQL",
                            "sqlExpression": "COUNT(*)",
                            "label": "row_count",
                        }
                    ],
                    "row_limit": 1,
                }
            ],
            "result_format": "json",
            "result_type": "full",
        }
    )
    try:
        return int(result["result"][0]["data"][0]["row_count"])
    except (KeyError, IndexError, TypeError, ValueError):
        raise SourceRejected("superset_result_invalid") from None


def probe_loaded(dataset, workspace_id, user_id):
    principal, direct_rows = direct_count(dataset)
    if principal != dataset["principal"]:
        raise SourceRejected("query_principal_mismatch")
    superset_rows = superset_count(dataset, user_id)
    if superset_rows != direct_rows:
        raise SourceRejected("principal_scope_mismatch")
    return {
        "dataset_id": str(dataset["dataset_id"]),
        "scope": {
            "workspace_id": workspace_id,
            "schema": dataset["schema"],
            "table": dataset["table"],
        },
        "principal": principal,
        "row_count": superset_rows,
        "evidence": {
            "superset_user": user_id,
            "same_registered_credential": True,
            "row_set_equivalent": True,
            "query_budget_ms": 3000,
        },
    }


def probe_dataset(app_connect, workspace_id, dataset_id, user_id):
    return probe_loaded(
        load_dataset(app_connect, workspace_id, dataset_id),
        workspace_id,
        user_id,
    )
