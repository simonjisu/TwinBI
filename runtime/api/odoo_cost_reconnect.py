"""Internal read-only preflight; no route, identity discovery, or registration writes."""
from dataclasses import dataclass
import re

import psycopg

from odoo_cost_guard import COMPANY, MAPPING, VIEW
from odoo_cost_mapping import inspect_cost_mapping
from source_policy import SourceRejected


@dataclass(frozen=True)
class ProviderTargetBinding:
    """Server-trusted provider attestation, never deserialized from a user request.

    The operator must authenticate this binding against a stable identity recorded
    before the address changed. Constructing this value does not establish trust.
    """
    source_id: str
    database_identity: str
    host: str
    port: int
    database: str
    username: str
    sslmode: str
    hostaddr: str


def inspect_cost_reconnect(config, registered, *, provider_target=None):
    """Inspect a provider-confirmed target without changing either input or storage.

    registered is a server-owned joined source/mapping/dataset snapshot using DB
    column names, plus provider_database_identity from a prior trusted receipt.
    Existing registrations have no such receipt and intentionally fail closed.
    """
    def require(condition, reason):
        if not condition:
            raise SourceRejected("odoo_cost_reconnect_" + reason)

    require(registered.get("source_kind") == "odoo19"
            and registered.get("mapping_id") == MAPPING
            and config.mapping_id == MAPPING, "mapping_required")
    require(registered.get("status") == "ready"
            and registered.get("mapping_status") == "validated", "validated_source_required")
    require(type(provider_target) is ProviderTargetBinding
            and isinstance(registered.get("provider_database_identity"), str)
            and bool(registered["provider_database_identity"])
            and isinstance(registered.get("source_id"), str)
            and bool(registered["source_id"]), "provider_identity_required")
    require(provider_target.source_id == registered["source_id"]
            and provider_target.database_identity == registered["provider_database_identity"]
            and isinstance(registered.get("hostaddr"), str) and bool(registered["hostaddr"])
            and isinstance(provider_target.hostaddr, str) and bool(provider_target.hostaddr),
            "provider_identity_mismatch")
    for name, stored in (("host", "host"), ("port", "port"), ("database", "database_name"),
                         ("username", "username"), ("sslmode", "sslmode")):
        value = registered.get(stored)
        require((type(value) is int and 1 <= value <= 65535) if name == "port"
                else (isinstance(value, str) and bool(value)), "endpoint_changed")
        require(getattr(config, name) == value == getattr(provider_target, name),
                "endpoint_changed")
    require(registered.get("query_principal") == config.username, "principal_changed")
    require(config.schema_name == registered.get("schema_name") == "public"
            and config.table_name == registered.get("table_name") == VIEW,
            "dataset_changed")
    require(type(config.company_id) is int and type(registered.get("company_id")) is int
            and config.company_id == registered["company_id"] == 1
            and config.company_name == registered.get("company_name") == COMPANY
            and config.scope_proof == registered.get("scope_proof") == "database_rls",
            "scope_changed")
    for name in ("schema_hash", "module_hash", "schema_fingerprint"):
        require(isinstance(registered.get(name), str)
                and re.fullmatch(r"sha256:[0-9a-f]{64}", registered[name]) is not None,
                "verified_pins_required")
    require(registered.get("view_equivalent") is True
            and registered.get("visible_scope_json") == {"companies": [1], "moves": [1], "lines": [1]}
            and isinstance(registered.get("columns_json"), list)
            and bool(registered["columns_json"]), "verified_pins_required")

    try:
        inspection = inspect_cost_mapping(config, expected_hostaddr=provider_target.hostaddr)
    except (psycopg.Error, OSError, ValueError, KeyError):
        raise SourceRejected("odoo_cost_reconnect_inspection_failed") from None
    require(inspection["hostaddr"] == provider_target.hostaddr, "provider_identity_mismatch")
    require(inspection["status"] == "validated" and not inspection["reasons"]
            and inspection["view_equivalent"] is True, "contract_unverified")
    for current, stored in (("principal", "query_principal"), ("mapping_id", "mapping_id"),
                            ("company_id", "company_id"), ("company_name", "company_name"),
                            ("scope_proof", "scope_proof"), ("schema_hash", "schema_hash"),
                            ("module_hash", "module_hash"), ("visible_scope", "visible_scope_json"),
                            ("dataset_schema_fingerprint", "schema_fingerprint"),
                            ("columns", "columns_json")):
        require(inspection[current] == registered[stored], "contract_changed")
    return inspection
