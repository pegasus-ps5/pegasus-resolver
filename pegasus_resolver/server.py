from __future__ import annotations

import argparse
import itertools
import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import monotonic
from typing import Any
from urllib.parse import urlparse

from . import __version__
from .providers import PROVIDERS, ResolveError, provider_for_url

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7799
REQUEST_LIMIT = 64 * 1024
DEFAULT_TIMEOUT_MS = 45_000
REQUEST_IDS = itertools.count(1)


class ResolverHandler(BaseHTTPRequestHandler):
    server_version = f"pegasus-resolver/{__version__}"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}", flush=True)

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/health":
            self.send_json(
                {
                    "status": "ok",
                    "version": __version__,
                    "providers": len(PROVIDERS),
                }
            )
        elif path == "/api/providers":
            self.send_json({"providers": [provider.metadata() for provider in PROVIDERS]})
        else:
            self.send_error_json(HTTPStatus.NOT_FOUND, "Route not found")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path != "/api/resolve":
            self.send_error_json(HTTPStatus.NOT_FOUND, "Route not found")
            return

        request_id = next(REQUEST_IDS)
        started_at = monotonic()
        try:
            payload = self.read_json_body()
            url = payload.get("url")
            timeout_ms = int(payload.get("timeoutMs") or DEFAULT_TIMEOUT_MS)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self.resolver_log(request_id, f"invalid request body: {exc}")
            self.send_error_json(HTTPStatus.BAD_REQUEST, f"Invalid JSON body: {exc}")
            return

        self.resolver_log(request_id, f"resolve requested url={url!r} timeoutMs={timeout_ms}")
        if not isinstance(url, str) or not valid_http_url(url):
            self.resolver_log(request_id, "rejected invalid url")
            self.send_error_json(HTTPStatus.BAD_REQUEST, "url must be an HTTP or HTTPS URL")
            return
        if timeout_ms < 5_000 or timeout_ms > 120_000:
            self.resolver_log(request_id, "rejected invalid timeoutMs")
            self.send_error_json(HTTPStatus.BAD_REQUEST, "timeoutMs must be between 5000 and 120000")
            return

        provider = provider_for_url(url)
        if provider is None:
            self.resolver_log(request_id, "no provider matched")
            self.send_error_json(HTTPStatus.NOT_FOUND, "Provider is not supported")
            return

        self.resolver_log(request_id, f"provider matched id={provider.id} name={provider.name}")
        try:
            resolved = provider.resolve(url, timeout_ms)
        except ResolveError as exc:
            elapsed_ms = int((monotonic() - started_at) * 1000)
            self.resolver_log(
                request_id,
                f"provider failed id={provider.id} elapsedMs={elapsed_ms} error={exc}",
            )
            self.send_error_json(HTTPStatus.BAD_GATEWAY, str(exc), provider=provider.id)
            return
        except Exception as exc:  # pragma: no cover - live provider failure path
            elapsed_ms = int((monotonic() - started_at) * 1000)
            self.resolver_log(
                request_id,
                f"provider crashed id={provider.id} elapsedMs={elapsed_ms} error={exc}",
            )
            self.send_error_json(
                HTTPStatus.BAD_GATEWAY,
                f"{provider.name} resolver failed: {exc}",
                provider=provider.id,
            )
            return

        elapsed_ms = int((monotonic() - started_at) * 1000)
        self.resolver_log(
            request_id,
            f"provider resolved id={provider.id} elapsedMs={elapsed_ms} fileName={resolved.file_name!r}",
        )
        self.send_json(resolved.as_json())

    def read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > REQUEST_LIMIT:
            raise ValueError("request body is empty or too large")
        body = self.rfile.read(length)
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(
        self,
        status: HTTPStatus,
        message: str,
        **extra: Any,
    ) -> None:
        payload: dict[str, Any] = {"error": message}
        payload.update(extra)
        self.send_json(payload, status)

    def resolver_log(self, request_id: int, message: str) -> None:
        print(f"[resolver:{request_id}] {message}", flush=True)


def valid_http_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc) and not parsed.fragment


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pegasus DL provider resolver")
    parser.add_argument(
        "--host",
        default=os.environ.get("PEGASUS_RESOLVER_HOST", DEFAULT_HOST),
        help=f"listen address, default {DEFAULT_HOST}",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PEGASUS_RESOLVER_PORT", DEFAULT_PORT)),
        help=f"listen port, default {DEFAULT_PORT}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    server = ThreadingHTTPServer((args.host, args.port), ResolverHandler)
    print(f"Pegasus Resolver listening on http://{args.host}:{args.port}", flush=True)
    print("Supported providers: " + ", ".join(provider.name for provider in PROVIDERS), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nPegasus Resolver stopped")
    finally:
        server.server_close()
    return 0
