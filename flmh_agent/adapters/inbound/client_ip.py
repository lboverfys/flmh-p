from __future__ import annotations

import hmac
import ipaddress

from starlette.requests import Request

from flmh_agent.config import Settings


def _address(value: str):
    if not value or value != value.strip() or "%" in value:
        raise ValueError("invalid_client_address")
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise ValueError("invalid_client_address") from error
    return address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped else address


def client_address(request: Request, settings: Settings) -> str:
    if request.client is None:
        raise ValueError("missing_client_address")
    peer = _address(request.client.host)
    trusted = any(peer in ipaddress.ip_network(cidr) for cidr in settings.trusted_proxy_cidrs)
    keys = request.headers.getlist("x-service-key")
    if not trusted or len(keys) != 1 or not settings.gateway_api_key or not hmac.compare_digest(
            keys[0].encode("utf-8"), settings.gateway_api_key.encode("utf-8")):
        return str(peer)
    forwarded = request.headers.getlist("x-flmh-client-ip")
    if not forwarded:
        return str(peer)
    if len(forwarded) != 1:
        raise ValueError("invalid_client_address")
    return str(_address(forwarded[0]))
