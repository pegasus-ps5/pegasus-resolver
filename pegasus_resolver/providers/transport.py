from __future__ import annotations

from http.client import HTTPConnection, HTTPSConnection
from urllib.parse import urlparse, urlunparse

from .base import ResolveError


def http_request(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout_ms: int,
) -> tuple[int, dict[str, str], bytes]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ResolveError("Resolver HTTP URL is invalid")

    path = urlunparse(("", "", parsed.path or "/", parsed.params, parsed.query, ""))
    connection_cls = HTTPSConnection if parsed.scheme == "https" else HTTPConnection
    connection = connection_cls(
        parsed.hostname,
        parsed.port,
        timeout=max(5.0, timeout_ms / 1000),
    )
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        status = response.status
        response_headers = {
            key.lower(): value for key, value in response.getheaders() if key and value
        }
        response_body = response.read()
    finally:
        connection.close()
    return status, response_headers, response_body


def add_cookie_header(headers: dict[str, str], cookies: list[dict[str, str]]) -> None:
    cookie_header = "; ".join(
        f"{item['name']}={item['value']}"
        for item in cookies
        if item.get("name") and item.get("value")
    )
    if cookie_header:
        headers["Cookie"] = cookie_header


def decode_response_body(body: bytes, headers: dict[str, str]) -> str:
    content_type = headers.get("content-type", "")
    charset = "utf-8"
    for part in content_type.split(";"):
        part = part.strip()
        if part.lower().startswith("charset="):
            charset = part.split("=", 1)[1].strip() or charset
    return body.decode(charset, "replace")
