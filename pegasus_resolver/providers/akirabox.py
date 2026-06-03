from __future__ import annotations

from html.parser import HTMLParser
from http.client import HTTPConnection, HTTPSConnection
from time import sleep
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

from .base import Provider, ResolveError, ResolvedDownload
from .utils import expires_at_from_url, file_name_from_url, html_from_page


class AkiraBoxPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.download_href: str | None = None
        self.file_url: str | None = None
        self._script_parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self._script_parts = []
            return
        if tag != "a":
            return

        values = {key.lower(): value or "" for key, value in attrs}
        if values.get("id") == "download-button" and values.get("href"):
            self.download_href = values["href"]

    def handle_data(self, data: str) -> None:
        if self._script_parts is not None:
            self._script_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "script" or self._script_parts is None:
            return

        script = "".join(self._script_parts)
        marker = "fileUrl"
        pos = script.find(marker)
        if pos >= 0 and self.file_url is None:
            quote_start = min(
                (idx for idx in (script.find('"', pos), script.find("'", pos)) if idx >= 0),
                default=-1,
            )
            if quote_start >= 0:
                quote = script[quote_start]
                quote_end = script.find(quote, quote_start + 1)
                if quote_end > quote_start:
                    self.file_url = script[quote_start + 1 : quote_end]

        self._script_parts = None


class AkiraBoxProvider(Provider):
    id = "akirabox"
    name = "AkiraBox"
    hosts = ("akirabox.com", "akirabox.to")

    def resolve(self, url: str, timeout_ms: int) -> ResolvedDownload:
        try:
            from scrapling.fetchers import StealthySession
        except ImportError as exc:
            raise ResolveError(
                "Scrapling fetchers are not installed. Run `python -m pip install -e .` "
                "from pegasus-resolver first."
            ) from exc

        page = None
        href = None
        with StealthySession(headless=True) as session:
            for attempt in range(3):
                page = session.fetch(
                    url,
                    network_idle=True,
                    wait_selector="#download-button[href]",
                    wait_selector_state="attached",
                    solve_cloudflare=attempt == 0,
                    timeout=timeout_ms,
                )
                href = akirabox_download_href_from_page(page)
                if href:
                    break
                sleep(1.5)

        if page is None:
            raise ResolveError("AkiraBox did not return a page")
        status = getattr(page, "status", 0)
        if status and (status < 200 or status >= 300):
            raise ResolveError(f"AkiraBox returned HTTP status {status}")
        if not href:
            raise ResolveError("AkiraBox download button was not found")

        page_url = str(getattr(page, "url", "") or url)
        resolved_url = urljoin(page_url, href)
        headers = akirabox_request_headers(page, url)
        cookies = akirabox_cookies(page)
        storage_url = akirabox_storage_url(
            resolved_url,
            headers,
            cookies,
            timeout_ms,
        )
        return ResolvedDownload(
            provider=self.id,
            url=storage_url,
            headers=headers,
            cookies=[],
            file_name=file_name_from_url(resolved_url),
            expires_at=expires_at_from_url(resolved_url),
        )


def akirabox_download_href_from_page(page: Any) -> str | None:
    parser = AkiraBoxPageParser()
    parser.feed(html_from_page(page))
    return parser.download_href


def akirabox_request_headers(page: Any, start_url: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    request_headers = getattr(page, "request_headers", {}) or {}
    user_agent = request_headers.get("user-agent") or request_headers.get("User-Agent")
    if user_agent:
        headers["User-Agent"] = user_agent
    headers["Referer"] = akirabox_referer_from_page(page, start_url)
    return headers


def akirabox_referer_from_page(page: Any, start_url: str) -> str:
    parser = AkiraBoxPageParser()
    parser.feed(html_from_page(page))
    if parser.file_url:
        return parser.file_url

    parsed = urlparse(start_url)
    if parsed.path:
        return f"https://akirabox.to{parsed.path}"
    return "https://akirabox.to/"


def akirabox_cookies(page: Any) -> list[dict[str, str]]:
    cookies = getattr(page, "cookies", ()) or ()
    resolved: list[dict[str, str]] = []
    for item in cookies:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        value = str(item.get("value") or "")
        if name == "cf_clearance" and value:
            resolved.append({"name": name, "value": value})
    return resolved


def akirabox_storage_url(
    download_url: str,
    headers: dict[str, str],
    cookies: list[dict[str, str]],
    timeout_ms: int,
) -> str:
    parsed = urlparse(download_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ResolveError("AkiraBox download URL is invalid")

    request_headers = dict(headers)
    cookie_header = "; ".join(
        f"{item['name']}={item['value']}"
        for item in cookies
        if item.get("name") and item.get("value")
    )
    if cookie_header:
        request_headers["Cookie"] = cookie_header
    request_headers.setdefault("Accept", "*/*")
    request_headers.setdefault("Accept-Encoding", "identity")

    path = urlunparse(("", "", parsed.path or "/", parsed.params, parsed.query, ""))
    connection_cls = HTTPSConnection if parsed.scheme == "https" else HTTPConnection
    connection = connection_cls(
        parsed.hostname,
        parsed.port,
        timeout=max(5.0, timeout_ms / 1000),
    )
    try:
        connection.request("GET", path, headers=request_headers)
        response = connection.getresponse()
        status = response.status
        location = response.getheader("Location")
        response.read(4096)
    finally:
        connection.close()

    if 300 <= status < 400 and location:
        return urljoin(download_url, location)
    if status in {200, 206}:
        return download_url
    raise ResolveError(f"AkiraBox download redirect returned HTTP status {status}")
