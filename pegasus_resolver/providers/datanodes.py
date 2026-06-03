from __future__ import annotations

import json
from dataclasses import dataclass
from html.parser import HTMLParser
from time import monotonic, sleep
from typing import Any
from urllib.parse import unquote, urlencode, urljoin, urlparse, urlunparse

from .base import Provider, ResolveError, ResolvedDownload
from .transport import add_cookie_header, decode_response_body, http_request
from .utils import (
    cookie_names_for_log,
    file_name_from_url,
    html_bool_attr,
    html_int_attr,
    provider_log,
    safe_url_for_log,
)


@dataclass(frozen=True)
class DataNodesDownloadForm:
    code: str
    countdown_seconds: int
    referer: str
    rand: str
    free_method: str
    premium_method: str
    has_password: bool
    has_captcha: bool
    has_countdown: bool
    message: str
    file_name: str | None = None


class DataNodesPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.form: DataNodesDownloadForm | None = None
        self.file_link: str | None = None
        self.file_code: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        if tag == "file-actions":
            self.file_link = values.get("link") or self.file_link
            self.file_code = values.get("code") or self.file_code
            return
        if tag != "download-countdown":
            return

        self.form = DataNodesDownloadForm(
            code=values.get("code", ""),
            countdown_seconds=html_int_attr(values.get(":countdown") or values.get("countdown")),
            referer=values.get("referer", ""),
            rand=values.get("rand", ""),
            free_method=values.get("free-method", ""),
            premium_method=values.get("premium-method", ""),
            has_password=html_bool_attr(values.get(":has-password") or values.get("has-password")),
            has_captcha=html_bool_attr(values.get(":has-captcha") or values.get("has-captcha")),
            has_countdown=html_bool_attr(
                values.get(":has-countdown") or values.get("has-countdown")
            ),
            message=values.get("message", ""),
            file_name=values.get("name") or file_name_from_url(self.file_link or ""),
        )


class DataNodesProvider(Provider):
    id = "datanodes"
    name = "DataNodes"
    hosts = ("datanodes.to",)

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
        with StealthySession(headless=True) as session:
            for attempt in range(3):
                attempt_started_at = monotonic()
                provider_log(
                    self.id,
                    f"scrapling fetch attempt={attempt + 1} solveCloudflare={attempt == 0}",
                )
                page = session.fetch(
                    url,
                    network_idle=False,
                    wait_selector="body",
                    wait_selector_state="attached",
                    solve_cloudflare=attempt == 0,
                    timeout=timeout_ms,
                )
                elapsed_ms = int((monotonic() - attempt_started_at) * 1000)
                provider_log(
                    self.id,
                    "scrapling fetch finished "
                    f"attempt={attempt + 1} elapsedMs={elapsed_ms} "
                    f"status={getattr(page, 'status', None)} "
                    f"url={safe_url_for_log(str(getattr(page, 'url', '') or url))}",
                )
                if page is not None:
                    break
                provider_log(self.id, f"scrapling fetch returned no page attempt={attempt + 1}")
                sleep(1.5)

        if page is None:
            raise ResolveError("DataNodes did not return a page")
        status = getattr(page, "status", 0)
        if status and (status < 200 or status >= 300):
            raise ResolveError(f"DataNodes returned HTTP status {status}")

        page_url = str(getattr(page, "url", "") or url)
        cookies = datanodes_cookies(page)
        provider_log(
            self.id,
            "browser page ready "
            f"pageUrl={safe_url_for_log(page_url)} "
            f"cookieNames={cookie_names_for_log(cookies)}",
        )
        request_headers = datanodes_request_headers(page)
        download_page_url = datanodes_download_page_url(page_url)
        provider_log(self.id, f"fetching download page url={safe_url_for_log(download_page_url)}")
        html = datanodes_fetch_download_page(
            download_page_url,
            request_headers,
            cookies,
            timeout_ms,
        )
        provider_log(self.id, f"download page fetched bytes={len(html.encode('utf-8'))}")
        form = datanodes_download_form_from_html(html)
        provider_log(
            self.id,
            "download form parsed "
            f"code={form.code!r} fileName={form.file_name!r} "
            f"countdown={form.countdown_seconds} hasCountdown={form.has_countdown} "
            f"hasCaptcha={form.has_captcha} hasPassword={form.has_password} "
            f"message={form.message!r}",
        )
        if form.has_password:
            raise ResolveError("DataNodes file requires a password")
        if form.has_captcha:
            raise ResolveError("DataNodes file requires a captcha")
        if form.has_countdown and form.countdown_seconds > 0:
            wait_seconds = form.countdown_seconds + 0.5
            if wait_seconds > max(5.0, timeout_ms / 1000):
                raise ResolveError("DataNodes countdown exceeds resolver timeout")
            provider_log(self.id, f"waiting countdown seconds={wait_seconds:.1f}")
            sleep(wait_seconds)

        provider_log(self.id, "posting download form")
        resolved_url = datanodes_post_download_url(
            download_page_url,
            form,
            request_headers,
            cookies,
            timeout_ms,
        )
        provider_log(self.id, f"download form returned url={safe_url_for_log(resolved_url)}")
        parsed = urlparse(resolved_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ResolveError("DataNodes returned an invalid download URL")

        elapsed_ms = int((monotonic() - started_at) * 1000)
        provider_log(self.id, f"finished elapsedMs={elapsed_ms}")
        return ResolvedDownload(
            provider=self.id,
            url=resolved_url,
            headers={},
            cookies=[],
            file_name=form.file_name or file_name_from_url(resolved_url),
        )


def datanodes_download_form_from_html(html: str) -> DataNodesDownloadForm:
    parser = DataNodesPageParser()
    parser.feed(html)
    form = parser.form
    if form is None:
        raise ResolveError("DataNodes download form was not found")
    if not form.code:
        raise ResolveError("DataNodes file code was not found")
    return form


def datanodes_cookies(page: Any) -> list[dict[str, str]]:
    cookies = getattr(page, "cookies", ()) or ()
    resolved: list[dict[str, str]] = []
    for item in cookies:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        value = str(item.get("value") or "")
        domain = str(item.get("domain") or "")
        if not name or not value:
            continue
        if domain and "datanodes.to" not in domain:
            continue
        resolved.append({"name": name, "value": value})
    return resolved


def datanodes_request_headers(page: Any) -> dict[str, str]:
    headers: dict[str, str] = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "identity",
    }
    request_headers = getattr(page, "request_headers", {}) or {}
    user_agent = request_headers.get("user-agent") or request_headers.get("User-Agent")
    if user_agent:
        headers["User-Agent"] = user_agent
    return headers


def datanodes_download_page_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ResolveError("DataNodes URL is invalid")
    return urlunparse((parsed.scheme, parsed.netloc, "/download", "", "", ""))


def datanodes_fetch_download_page(
    url: str,
    headers: dict[str, str],
    cookies: list[dict[str, str]],
    timeout_ms: int,
) -> str:
    request_headers = dict(headers)
    add_cookie_header(request_headers, cookies)
    provider_log("datanodes", f"GET {safe_url_for_log(url)}")
    status, response_headers, body = http_request(
        "GET",
        url,
        request_headers,
        None,
        timeout_ms,
    )
    provider_log(
        "datanodes",
        "GET response "
        f"status={status} contentType={response_headers.get('content-type', '')!r} "
        f"bytes={len(body)}",
    )
    if status != 200:
        raise ResolveError(f"DataNodes download page returned HTTP status {status}")
    return decode_response_body(body, response_headers)


def datanodes_post_download_url(
    url: str,
    form: DataNodesDownloadForm,
    headers: dict[str, str],
    cookies: list[dict[str, str]],
    timeout_ms: int,
) -> str:
    payload = {
        "op": "download2",
        "id": form.code,
        "rand": form.rand,
        "referer": form.referer,
        "method_free": form.free_method,
        "method_premium": form.premium_method,
        "g_captch__a": "1",
    }
    body = urlencode(payload).encode("utf-8")
    request_headers = dict(headers)
    request_headers.update(
        {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/x-www-form-urlencoded",
            "Content-Length": str(len(body)),
            "Origin": datanodes_origin(url),
            "Referer": url,
        }
    )
    add_cookie_header(request_headers, cookies)
    provider_log(
        "datanodes",
        "POST download form "
        f"url={safe_url_for_log(url)} code={form.code!r} "
        f"bodyBytes={len(body)} cookieNames={cookie_names_for_log(cookies)}",
    )
    status, response_headers, response_body = http_request(
        "POST",
        url,
        request_headers,
        body,
        timeout_ms,
    )
    provider_log(
        "datanodes",
        "POST response "
        f"status={status} contentType={response_headers.get('content-type', '')!r} "
        f"bytes={len(response_body)}",
    )
    if 300 <= status < 400:
        location = response_headers.get("location")
        if location:
            provider_log("datanodes", f"POST redirect location={safe_url_for_log(location)}")
            return urljoin(url, location)
    if status != 200:
        raise ResolveError(f"DataNodes download form returned HTTP status {status}")

    text = decode_response_body(response_body, response_headers)
    try:
        payload_json = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ResolveError("DataNodes returned invalid JSON") from exc
    if not isinstance(payload_json, dict):
        raise ResolveError("DataNodes returned invalid JSON")
    error = payload_json.get("error")
    if error:
        raise ResolveError(f"DataNodes returned an error: {error}")
    resolved_url = payload_json.get("url")
    if not isinstance(resolved_url, str) or not resolved_url:
        raise ResolveError("DataNodes did not return a download URL")
    decoded_url = unquote(resolved_url)
    provider_log("datanodes", f"POST JSON url={safe_url_for_log(decoded_url)}")
    return decoded_url


def datanodes_origin(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ResolveError("DataNodes URL is invalid")
    return urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
