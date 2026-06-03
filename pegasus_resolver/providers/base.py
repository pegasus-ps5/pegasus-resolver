from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .utils import normalized_host


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
