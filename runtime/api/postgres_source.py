import hashlib
import json
from urllib.parse import quote_plus

import psycopg
from psycopg import sql
from source_policy import SourceRejected, source_target


def connect_postgresql(config, *, expected_hostaddr=None):
    target, _ = source_target(config.host, config.port, config.sslmode)
    if expected_hostaddr is not None and target["hostaddr"] != expected_hostaddr:
        raise SourceRejected("source_address_changed")
    try:
        return target["hostaddr"], psycopg.connect(
            host=config.host,
            port=config.port,
            dbname=config.database,
            user=config.username,
            password=config.password,
            **target,
            connect_timeout=5,
        )
    except psycopg.Error:
        raise SourceRejected("connection_failed") from None


def inspect_principal(connection, config, schema_name):
    connection.execute("SET TRANSACTION READ ONLY")
    connection.execute("SET LOCAL statement_timeout = '3s'")
    connection.execute("SET LOCAL lock_timeout = '1s'")
    identity = connection.execute(
        "SELECT current_user, current_database(), "
        "current_setting('transaction_read_only')"
    ).fetchone()
    if identity[0] != config.username or identity[2] != "on":
        raise SourceRejected("principal_mismatch")

    roles = connection.execute(
        """
        SELECT rolname, rolsuper, rolbypassrls, rolcreatedb,
               rolcreaterole, rolreplication
        FROM pg_roles
        WHERE pg_has_role(current_user, oid, 'MEMBER')
        """
    ).fetchall()
    if any(any(row[1:]) for row in roles):
        raise SourceRejected("privileged_role")

    database_privileges = connection.execute(
        """
        SELECT
            pg_get_userbyid(datdba) = current_user,
            has_database_privilege(current_user, current_database(), 'CREATE'),
            has_database_privilege(current_user, current_database(), 'TEMP')
        FROM pg_database
        WHERE datname = current_database()
        """
    ).fetchone()
    if any(database_privileges):
        raise SourceRejected("database_write_capability")

    schema_privileges = connection.execute(
        """
        SELECT
            pg_get_userbyid(nspowner) = current_user,
            has_schema_privilege(current_user, nspname, 'CREATE')
        FROM pg_namespace
        WHERE nspname = %s
        """,
        (schema_name,),
    ).fetchone()
    if schema_privileges is None:
        raise SourceRejected("schema_missing")
    if any(schema_privileges):
        raise SourceRejected("schema_write_capability")
    return identity


def inspect_postgresql(config):
    hostaddr, connection = connect_postgresql(config)
    try:
        with connection:
            identity = inspect_principal(connection, config, config.schema_name)

            relation = connection.execute(
                """
                SELECT
                    relation.relkind,
                    pg_get_userbyid(relation.relowner) = current_user,
                    greatest(relation.reltuples, 0)::bigint,
                    pg_total_relation_size(relation.oid),
                    relation.relrowsecurity, relation.relforcerowsecurity
                FROM pg_class AS relation
                JOIN pg_namespace AS namespace
                  ON namespace.oid = relation.relnamespace
                WHERE namespace.nspname = %s AND relation.relname = %s
                """,
                (config.schema_name, config.table_name),
            ).fetchone()
            if relation is None:
                raise SourceRejected("relation_missing")
            if relation[0] not in {"r", "p"}:
                raise SourceRejected("relation_kind_not_allowed")
            if relation[1]:
                raise SourceRejected("relation_owned")
            if getattr(config, "require_rls_disabled", False) and any(relation[4:6]):
                raise SourceRejected("fixture_row_security_changed")

            qualified = sql.Identifier(
                config.schema_name,
                config.table_name,
            ).as_string(connection)
            privileges = connection.execute(
                """
                SELECT
                    has_table_privilege(current_user, %s, 'SELECT'),
                    has_table_privilege(current_user, %s, 'INSERT'),
                    has_table_privilege(current_user, %s, 'UPDATE'),
                    has_table_privilege(current_user, %s, 'DELETE'),
                    has_table_privilege(current_user, %s, 'TRUNCATE'),
                    has_table_privilege(current_user, %s, 'REFERENCES'),
                    has_table_privilege(current_user, %s, 'TRIGGER')
                """,
                (qualified,) * 7,
            ).fetchone()
            if not privileges[0]:
                raise SourceRejected("select_missing")
            if any(privileges[1:]):
                raise SourceRejected("table_write_capability")

            columns = connection.execute(
                """
                SELECT column_name, data_type, is_nullable, ordinal_position
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                ORDER BY ordinal_position
                """,
                (config.schema_name, config.table_name),
            ).fetchall()
            if not columns:
                raise SourceRejected("columns_missing")
    finally:
        connection.close()

    fingerprint = hashlib.sha256(
        json.dumps(columns, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "hostaddr": hostaddr,
        "principal": identity[0],
        "estimated_rows": relation[2],
        "relation_bytes": relation[3],
        "schema_fingerprint": f"sha256:{fingerprint}",
        "columns": [
            {
                "name": row[0],
                "type": row[1],
                "nullable": row[2] == "YES",
                "position": row[3],
            }
            for row in columns
        ],
    }


def superset_database_payload(config, hostaddr, name):
    target, _ = source_target(config.host, config.port, config.sslmode)
    if target["hostaddr"] != str(hostaddr):
        raise SourceRejected("source_address_changed")
    username = quote_plus(config.username)
    password = quote_plus(config.password)
    database = quote_plus(config.database)
    uri = (
        f"postgresql+psycopg2://{username}:{password}"
        f"@{config.host}:{config.port}/{database}?sslmode={config.sslmode}"
    )
    from odoo_cost_guard import APPLICATION, MAPPING
    cost_args = {"application_name": APPLICATION} if getattr(config, "mapping_id", None) == MAPPING else {}
    return {
        "database_name": name,
        "sqlalchemy_uri": uri,
        "expose_in_sqllab": False,
        "allow_ctas": False,
        "allow_cvas": False,
        "allow_dml": False,
        "extra": json.dumps(
            {
                "engine_params": {
                    "connect_args": {
                        **target,
                        **cost_args,
                        "options": (
                            "-c default_transaction_read_only=on "
                            "-c statement_timeout=5000"
                        ),
                    }
                }
            },
            separators=(",", ":"),
        ),
    }
