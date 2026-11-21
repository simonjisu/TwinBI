"""Operator-owned PostgreSQL endpoint policy, shared by API and Superset."""

import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket


class SourceRejected(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


FIXTURE_HOSTS = {"fixture-db", "managed-db", "odoo-db", "odoo-cost-db"}
PRIVATE_NETWORKS = tuple(map(ipaddress.ip_network, (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7",
)))


def bounded_read(path, maximum):
    with path.open("rb") as file:
        raw = file.read(maximum + 1)
    if not 1 <= len(raw) <= maximum:
        raise ValueError
    return raw


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def endpoint_policy(host, port):
    if host in FIXTURE_HOSTS and port == 5432:
        return None
    directory = Path(os.environ.get("TWINBI_SOURCE_POLICY_DIR", "/app/source-policy"))
    try:
        raw = bounded_read(directory / "endpoints.json", 65536)
        entries = json.loads(raw, object_pairs_hook=unique_object)
        if not isinstance(entries, list) or len(entries) > 100:
            raise ValueError
        approved = {}
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {
                "host", "port", "networks", "ca_file",
            }:
                raise ValueError
            name, number = entry["host"], entry["port"]
            if (
                not isinstance(name, str)
                or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", name)
                or any(
                    not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
                    for part in name.split(".")
                )
                or name in FIXTURE_HOSTS
                or type(number) is not int or not 1 <= number <= 65535
                or (name, number) in approved
            ):
                raise ValueError
            networks = entry["networks"]
            if not isinstance(networks, list) or not 1 <= len(networks) <= 32:
                raise ValueError
            if any(not isinstance(value, str) or "/" not in value for value in networks):
                raise ValueError
            networks = [ipaddress.ip_network(value, strict=True) for value in networks]
            filename = entry["ca_file"]
            if (
                not isinstance(filename, str) or len(filename) > 128
                or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*\.pem", filename)
            ):
                raise ValueError
            ca = directory / filename
            if ca.is_symlink() or not ca.is_file():
                raise ValueError
            certificate = bounded_read(ca, 1048576)
            approved[(name, number)] = (
                networks, str(ca), hashlib.sha256(certificate).hexdigest(),
            )
    except (OSError, ValueError, TypeError, KeyError):
        raise SourceRejected("source_policy_invalid") from None
    if (host, port) not in approved:
        raise SourceRejected("host_not_allowed")
    return approved[(host, port)]


def source_target(host, port, sslmode):
    policy = endpoint_policy(host, port)
    if policy is not None and sslmode != "verify-full":
        raise SourceRejected("tls_verify_full_required")
    try:
        addresses = sorted({
            str(ipaddress.ip_address(item[4][0]))
            for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        })
    except (socket.gaierror, ValueError):
        raise SourceRejected("host_unresolved") from None
    if not addresses:
        raise SourceRejected("host_unresolved")
    for value in addresses:
        address = ipaddress.ip_address(value)
        if (
            address.is_loopback or address.is_link_local or address.is_multicast
            or address.is_unspecified or address.is_reserved
            or getattr(address, "ipv4_mapped", None)
            or value in {"100.100.100.200", "fd00:ec2::254"}
            or not (
                address.is_global
                or any(address in network for network in PRIVATE_NETWORKS)
            )
        ):
            raise SourceRejected("reserved_address")
        if policy is not None and not any(address in network for network in policy[0]):
            raise SourceRejected("address_not_allowed")
    options = {"hostaddr": addresses[0], "sslmode": sslmode}
    if policy is not None:
        options["sslrootcert"] = policy[1]
    return options, policy[2] if policy else None
