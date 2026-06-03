from __future__ import annotations

from html.parser import HTMLParser
from time import sleep
from typing import Any
from urllib.parse import urljoin

from .base import Provider, ResolveError, ResolvedDownload
from .utils import file_name_from_url, html_from_page


class DownloadLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.download_href: str | None = None
        self._candidate_href: str | None = None
        self._candidate_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        values = {key.lower(): value or "" for key, value in attrs}
        href = values.get("href", "")
        if values.get("id") == "download-link" and href:
            self.download_href = href
            return
        classes = set(values.get("class", "").split())
        if href and "button" in classes:
            self._candidate_href = href
            self._candidate_text = []

    def handle_data(self, data: str) -> None:
        if self._candidate_href:
            self._candidate_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or not self._candidate_href:
            return
        text = " ".join(part.strip() for part in self._candidate_text).strip().lower()
        if "download" in text and "usenet" not in text and not self.download_href:
            self.download_href = self._candidate_href
        self._candidate_href = None
        self._candidate_text = []


class VikingFileProvider(Provider):
    id = "vikingfile"
    name = "VikingFile"
    hosts = ("vik1ngfile.site", "vikingfile.com")

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
                    wait_selector="#download-link[href]",
                    wait_selector_state="attached",
                    solve_cloudflare=attempt == 0,
                    timeout=timeout_ms,
                )
                href = download_href_from_page(page)
                if href:
                    break
                sleep(1.5)

        if page is None:
            raise ResolveError("VikingFile did not return a page")
        status = getattr(page, "status", 0)
        if status and (status < 200 or status >= 300):
            raise ResolveError(f"VikingFile returned HTTP status {status}")
        if not href:
            raise ResolveError("VikingFile download link was not found")

        page_url = str(getattr(page, "url", "") or url)
        resolved_url = urljoin(page_url, href)
        return ResolvedDownload(
            provider=self.id,
            url=resolved_url,
            headers={},
            cookies=[],
            file_name=file_name_from_url(resolved_url),
        )


def download_href_from_page(page: Any) -> str | None:
    html = html_from_page(page)
    parser = DownloadLinkParser()
    parser.feed(html)
    return parser.download_href
