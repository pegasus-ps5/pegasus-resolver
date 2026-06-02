from __future__ import annotations

import threading
import unittest
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, quote

from pegasus_resolver.providers import (
    AkiraBoxProvider,
    DataNodesProvider,
    akirabox_cookies,
    akirabox_download_href_from_page,
    akirabox_referer_from_page,
    akirabox_storage_url,
    datanodes_download_form_from_html,
    datanodes_post_download_url,
    expires_at_from_url,
    provider_for_url,
)


class DummyPage:
    def __init__(
        self,
        html: str,
        *,
        cookies: tuple[dict[str, object], ...] = (),
    ) -> None:
        self.body = html.encode("utf-8")
        self.encoding = "utf-8"
        self.cookies = cookies


class AkiraBoxProviderTest(unittest.TestCase):
    def test_provider_matches_akirabox_hosts(self) -> None:
        provider = provider_for_url("https://akirabox.com/dZxG5eKRRGVj/file")
        self.assertIsInstance(provider, AkiraBoxProvider)
        self.assertIs(provider_for_url("https://www.akirabox.to/abc/file"), provider)

    def test_download_href_parser_finds_button_link(self) -> None:
        page = DummyPage(
            '<a href="/ignored">Download</a>'
            '<a id="download-button" href="https://akirabox.to/download/token/'
            'PPSA09804.exfat?expiration=1780310660&amp;signature=abc">'
            "Download</a>"
        )

        href = (
            "https://akirabox.to/download/token/PPSA09804.exfat"
            "?expiration=1780310660&signature=abc"
        )
        self.assertEqual(
            akirabox_download_href_from_page(page),
            href,
        )

    def test_referer_prefers_file_url_script(self) -> None:
        page = DummyPage(
            """
            <script>
              const fileUrl = "https://akirabox.to/dZxG5eKRRGVj/file";
            </script>
            """
        )

        self.assertEqual(
            akirabox_referer_from_page(page, "https://akirabox.com/dZxG5eKRRGVj/file"),
            "https://akirabox.to/dZxG5eKRRGVj/file",
        )

    def test_cookies_only_return_cloudflare_clearance(self) -> None:
        page = DummyPage(
            "",
            cookies=(
                {"name": "cf_clearance", "value": "token"},
                {"name": "akira_box_user_session", "value": "session"},
                {"name": "_ga", "value": "analytics"},
            ),
        )

        self.assertEqual(akirabox_cookies(page), [{"name": "cf_clearance", "value": "token"}])

    def test_expiration_query_becomes_iso_timestamp(self) -> None:
        self.assertEqual(
            expires_at_from_url("https://akirabox.to/download/file?expiration=1780310660"),
            "2026-06-01T10:44:20Z",
        )

    def test_storage_url_captures_redirect_location(self) -> None:
        seen_headers: dict[str, str] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                seen_headers["referer"] = self.headers.get("Referer", "")
                seen_headers["cookie"] = self.headers.get("Cookie", "")
                self.send_response(HTTPStatus.FOUND)
                self.send_header("Location", "/storage/file.pkg?access=ok")
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/download/file.pkg"
            self.assertEqual(
                akirabox_storage_url(
                    url,
                    {"Referer": "https://akirabox.to/dZxG5eKRRGVj/file"},
                    [{"name": "cf_clearance", "value": "token"}],
                    5000,
                ),
                f"http://127.0.0.1:{server.server_port}/storage/file.pkg?access=ok",
            )
            self.assertEqual(seen_headers["referer"], "https://akirabox.to/dZxG5eKRRGVj/file")
            self.assertEqual(seen_headers["cookie"], "cf_clearance=token")
        finally:
            server.shutdown()
            thread.join()
            server.server_close()


class DataNodesProviderTest(unittest.TestCase):
    def test_provider_matches_datanodes_host(self) -> None:
        provider = provider_for_url("https://datanodes.to/5yemvzjazz35")
        self.assertIsInstance(provider, DataNodesProvider)
        self.assertIs(provider_for_url("https://www.datanodes.to/abc"), provider)

    def test_download_form_parser_finds_component_props(self) -> None:
        form = datanodes_download_form_from_html(
            """
            <file-actions link="https://datanodes.to/5yemvzjazz35/PPSA18191.exfat"
                code="5yemvzjazz35"></file-actions>
            <download-countdown :countdown="5" code="5yemvzjazz35"
                referer="" rand="" free-method="" premium-method=""
                :has-password="false" :has-captcha="false"
                :has-countdown="true" message="" name="PPSA18191.exfat">
            </download-countdown>
            """
        )

        self.assertEqual(form.code, "5yemvzjazz35")
        self.assertEqual(form.countdown_seconds, 5)
        self.assertTrue(form.has_countdown)
        self.assertFalse(form.has_password)
        self.assertFalse(form.has_captcha)
        self.assertEqual(form.file_name, "PPSA18191.exfat")

    def test_post_download_url_sends_provider_form(self) -> None:
        seen_body: dict[str, list[str]] = {}
        seen_headers: dict[str, str] = {}
        direct_url = "https://node42.datanodes.to:8443/d/token/PPSA18191.exfat"

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length).decode("utf-8")
                seen_body.update(parse_qs(body, keep_blank_values=True))
                seen_headers["content_type"] = self.headers.get("Content-Type", "")
                seen_headers["cookie"] = self.headers.get("Cookie", "")
                seen_headers["origin"] = self.headers.get("Origin", "")
                seen_headers["referer"] = self.headers.get("Referer", "")
                response = ('{"url":"' + quote(direct_url, safe="") + '"}').encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, format: str, *args: object) -> None:
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            form = datanodes_download_form_from_html(
                """
                <download-countdown :countdown="0" code="5yemvzjazz35"
                    referer="" rand="" free-method="" premium-method=""
                    :has-password="false" :has-captcha="false"
                    :has-countdown="false" name="PPSA18191.exfat">
                </download-countdown>
                """
            )
            url = f"http://127.0.0.1:{server.server_port}/download"
            self.assertEqual(
                datanodes_post_download_url(
                    url,
                    form,
                    {"User-Agent": "Pegasus Resolver Test"},
                    [{"name": "file_code", "value": "5yemvzjazz35"}],
                    5000,
                ),
                direct_url,
            )
            self.assertEqual(seen_body["op"], ["download2"])
            self.assertEqual(seen_body["id"], ["5yemvzjazz35"])
            self.assertEqual(seen_body["g_captch__a"], ["1"])
            self.assertEqual(
                seen_headers["content_type"],
                "application/x-www-form-urlencoded",
            )
            self.assertEqual(seen_headers["cookie"], "file_code=5yemvzjazz35")
            self.assertEqual(
                seen_headers["origin"],
                f"http://127.0.0.1:{server.server_port}",
            )
            self.assertEqual(seen_headers["referer"], url)
        finally:
            server.shutdown()
            thread.join()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
