from __future__ import annotations

import threading

from .base import Auth, Cancelled, Context, Item, Source
from .custom import custom_sources
from .faa import faa_sources
from .local import local_sources
from .services import public_service_sources

__all__ = ["Auth", "Cancelled", "Context", "Item", "Source", "registry", "refresh"]

_lock = threading.Lock()
_registry: dict[str, Source] | None = None


def refresh(settings) -> dict[str, Source]:
    global _registry
    with _lock:
        srcs = [*faa_sources(), *public_service_sources(), *local_sources(settings), *custom_sources(settings)]
        _registry = {s.id: s for s in srcs}
        return _registry


def registry(settings) -> dict[str, Source]:
    return _registry if _registry is not None else refresh(settings)
