from __future__ import annotations

from html.parser import HTMLParser
from time import monotonic, sleep
from typing import Any, Mapping
from urllib.parse import urljoin, urlparse

from .base import Provider, ResolveError, ResolvedDownload
from .utils import (
    file_name_from_url,
    html_from_page,
    provider_log,
    safe_url_for_log,
)


class BuzzHeavierPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.download_href: str | None = None
        self.file_name: str | None = None
        self._title_parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        if tag == "title":
            self._title_parts = []
            return
        if tag == "meta" and values.get("name", "").lower() == "title":
            self.file_name = values.get("content") or self.file_name
            return
        if tag != "a":
            return

        hx_get = values.get("hx-get", "")
        if hx_get and "/download" in hx_get and self.download_href is None:
            self.download_href = hx_get

    def handle_data(self, data: str) -> None:
        if self._title_parts is not None:
            self._title_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "title" or self._title_parts is None:
            return

        title = "".join(self._title_parts).strip()
        if title and self.file_name is None:
            self.file_name = title
        self._title_parts = None


class BuzzHeavierProvider(Provider):
    id = "buzzheavier"
    name = "BuzzHeavier"
    hosts = ("buzzheavier.com", "bzzhr.co", "bzzhr.to")

    def resolve(self, url: str, timeout_ms: int) -> ResolvedDownload:
        started_at = monotonic()
        provider_log(self.id, f"start url={safe_url_for_log(url)} timeoutMs={timeout_ms}")
        try:
            from scrapling.fetchers import StealthySession
        except ImportError as exc:
            raise ResolveError(
                "Scrapling fetchers are not installed. Run `python -m pip install -e .` "
                "from pegasus-resolver first."
            ) from exc

        page = None
        download_href = None
        file_name = None
        with StealthySession(headless=True) as session:
            for attempt in range(3):
                provider_log(
                    self.id,
                    f"scrapling fetch attempt={attempt + 1} solveCloudflare={attempt == 0}",
                )
                page = session.fetch(
                    url,
                    network_idle=True,
                    wait_selector='a[hx-get*="/download"]',
                    wait_selector_state="attached",
                    solve_cloudflare=attempt == 0,
                    timeout=timeout_ms,
                )
                parsed_page = buzzheavier_page_from_page(page)
                download_href = parsed_page.download_href
                file_name = parsed_page.file_name
                if download_href:
                    break
                sleep(1.5)

            if page is None:
                raise ResolveError("BuzzHeavier did not return a page")
            status = getattr(page, "status", 0)
            if status and (status < 200 or status >= 300):
                raise ResolveError(f"BuzzHeavier returned HTTP status {status}")
            if not download_href:
                raise ResolveError("BuzzHeavier download link was not found")

            page_url = str(getattr(page, "url", "") or url)
            signed_download_url = urljoin(page_url, download_href)
            provider_log(
                self.id,
                "download link parsed "
                f"pageUrl={safe_url_for_log(page_url)} "
                f"downloadUrl={safe_url_for_log(signed_download_url)} "
                f"fileName={file_name!r}",
            )
            resolved_url = buzzheavier_hx_redirect(
                session.context,
                page_url,
                signed_download_url,
                timeout_ms,
            )

        parsed = urlparse(resolved_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ResolveError("BuzzHeavier returned an invalid download URL")

        elapsed_ms = int((monotonic() - started_at) * 1000)
        provider_log(
            self.id,
            f"finished elapsedMs={elapsed_ms} url={safe_url_for_log(resolved_url)}",
        )
        return ResolvedDownload(
            provider=self.id,
            url=resolved_url,
            headers={},
            cookies=[],
            file_name=file_name or file_name_from_url(resolved_url),
        )


def buzzheavier_page_from_page(page: Any) -> BuzzHeavierPageParser:
    parser = BuzzHeavierPageParser()
    parser.feed(html_from_page(page))
    return parser


def buzzheavier_download_href_from_page(page: Any) -> str | None:
    return buzzheavier_page_from_page(page).download_href


def buzzheavier_file_name_from_page(page: Any) -> str | None:
    return buzzheavier_page_from_page(page).file_name


def buzzheavier_hx_redirect(
    context: Any,
    page_url: str,
    signed_download_url: str,
    timeout_ms: int,
) -> str:
    browser_page = context.new_page()
    try:
        browser_page.goto(page_url, wait_until="domcontentloaded", timeout=timeout_ms)
        result = browser_page.evaluate(
            """
            async ({ signedDownloadUrl, pageUrl }) => {
              const response = await fetch(signedDownloadUrl, {
                method: 'GET',
                redirect: 'manual',
                headers: {
                  'Accept': '*/*',
                  'HX-Request': 'true',
                  'HX-Current-URL': pageUrl
                }
              });
              const headers = {};
              for (const [key, value] of response.headers.entries()) {
                headers[key] = value;
              }
              return {
                status: response.status,
                type: response.type,
                url: response.url,
                headers
              };
            }
            """,
            {"signedDownloadUrl": signed_download_url, "pageUrl": page_url},
        )
    finally:
        browser_page.close()

    return buzzheavier_redirect_from_fetch_result(result)


def buzzheavier_redirect_from_fetch_result(result: Mapping[str, Any]) -> str:
    try:
        status = int(result.get("status") or 0)
    except (TypeError, ValueError):
        status = 0
    if status not in {200, 204}:
        raise ResolveError(f"BuzzHeavier download request returned HTTP status {status}")

    headers = result.get("headers")
    if not isinstance(headers, Mapping):
        raise ResolveError("BuzzHeavier download request returned invalid headers")
    redirect = headers.get("hx-redirect") or headers.get("HX-Redirect")
    if not isinstance(redirect, str) or not redirect:
        raise ResolveError("BuzzHeavier download redirect was not found")
    return redirect
