from __future__ import annotations

from .akirabox import AkiraBoxProvider
from .base import Provider
from .datanodes import DataNodesProvider
from .vikingfile import VikingFileProvider


PROVIDERS: tuple[Provider, ...] = (
    AkiraBoxProvider(),
    DataNodesProvider(),
    VikingFileProvider(),
)


def provider_for_url(url: str) -> Provider | None:
    for provider in PROVIDERS:
        if provider.matches(url):
            return provider
    return None
