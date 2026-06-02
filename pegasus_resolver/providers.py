from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.client import HTTPConnection, HTTPSConnection
from time import monotonic, sleep
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse, urlunparse


class ResolveError(RuntimeError):
    """Raised when a provider cannot resolve a URL."""


@dataclass(frozen=True)
class ResolvedDownload:
    provider: str
    url: str
    headers: dict[str, str]
    cookies: list[dict[str, str]]
    file_name: str | None = None
    expires_at: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "url": self.url,
            "headers": self.headers,
            "cookies": self.cookies,
            "fileName": self.file_name,
            "expiresAt": self.expires_at,
        }


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


class Provider:
    id: str
    name: str
    hosts: tuple[str, ...]

    def matches(self, url: str) -> bool:
        host = normalized_host(url)
        return any(host == item or host.endswith(f".{item}") for item in self.hosts)

    def metadata(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "hosts": list(self.hosts),
        }

    def resolve(self, url: str, timeout_ms: int) -> ResolvedDownload:
        raise NotImplementedError


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


PROVIDERS: tuple[Provider, ...] = (
    AkiraBoxProvider(),
    DataNodesProvider(),
    VikingFileProvider(),
)


def download_href_from_page(page: Any) -> str | None:
    html = html_from_page(page)
    parser = DownloadLinkParser()
    parser.feed(html)
    return parser.download_href


def akirabox_download_href_from_page(page: Any) -> str | None:
    parser = AkiraBoxPageParser()
    parser.feed(html_from_page(page))
    return parser.download_href


def datanodes_download_form_from_html(html: str) -> DataNodesDownloadForm:
    parser = DataNodesPageParser()
    parser.feed(html)
    form = parser.form
    if form is None:
        raise ResolveError("DataNodes download form was not found")
    if not form.code:
        raise ResolveError("DataNodes file code was not found")
    return form


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


def datanodes_origin(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ResolveError("DataNodes URL is invalid")
    return urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))


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


def provider_for_url(url: str) -> Provider | None:
    for provider in PROVIDERS:
        if provider.matches(url):
            return provider
    return None


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
