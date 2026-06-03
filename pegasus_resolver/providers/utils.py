from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse, urlunparse


def provider_log(provider: str, message: str) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    print(f"[{now}] INFO: {provider}: {message}", flush=True)


def cookie_names_for_log(cookies: list[dict[str, str]]) -> list[str]:
    return [item["name"] for item in cookies if item.get("name")]


def safe_url_for_log(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return url
    path = parsed.path or "/"
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 3 and parts[0] == "d":
        path = f"/d/<redacted>/{parts[-1]}"
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def html_from_page(page: Any) -> str:
    body = getattr(page, "body", b"")
    if isinstance(body, bytes):
        return body.decode(getattr(page, "encoding", None) or "utf-8", "replace")
    return str(body or getattr(page, "html_content", "") or "")


def normalized_host(url: str) -> str:
    parsed = urlparse(url)
    return (parsed.hostname or "").lower().removeprefix("www.")


def file_name_from_url(url: str) -> str | None:
    path = urlparse(url).path.rstrip("/")
    if not path:
        return None
    name = unquote(path.rsplit("/", 1)[-1])
    return name or None


def html_int_attr(value: str | None) -> int:
    try:
        return int(str(value or "0"))
    except (TypeError, ValueError):
        return 0


def html_bool_attr(value: str | None) -> bool:
    if value is None:
        return False
    normalized = str(value).strip().lower()
    return normalized in {"", "1", "true", "yes"}


def expires_at_from_url(url: str) -> str | None:
    values = parse_qs(urlparse(url).query).get("expiration")
    if not values:
        return None
    try:
        expires = int(values[0])
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(expires, timezone.utc).isoformat().replace("+00:00", "Z")
