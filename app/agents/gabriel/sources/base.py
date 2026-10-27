"""Discovery source interface and provider registry.

A source performs real research against a publicly accessible provider
using legitimate, documented access methods -- no bypassed auth,
CAPTCHAs, paywalls, or access restrictions. When a provider is
unavailable the source raises instead of fabricating results.

To add a provider: implement :class:`DiscoverySource` in a new module
under this package and register it in :data:`SOURCES`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class SourceError(RuntimeError):
    """A discovery provider failed or returned unusable data."""


class SourceTimeout(SourceError):
    """A discovery provider did not answer within the time budget."""


class UnknownSourceError(SourceError):
    """The requested discovery provider is not registered."""


@dataclass(frozen=True)
class BoundingBox:
    """South/west/north/east bounds in decimal degrees."""

    south: float
    west: float
    north: float
    east: float

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.south, self.west, self.north, self.east)


@dataclass
class RawListing:
    """One unvalidated candidate observed at a provider.

    Fields hold observed values only; anything the provider did not
    supply stays None. ``category_hint`` is the provider-side signal
    (e.g. a building tag), mapped to a canonical category later.
    """

    source: str
    external_id: str
    element_url: str | None = None
    name: str | None = None
    street_address: str | None = None
    city: str | None = None
    state: str | None = None
    zip_code: str | None = None
    category_hint: str = "other"
    latitude: float | None = None
    longitude: float | None = None
    retrieved_at: str = ""
    tags: dict[str, Any] = field(default_factory=dict)


class DiscoverySource(Protocol):
    """Interface every discovery provider implements."""

    name: str
    description: str
    docs_url: str
    requires_credentials: bool

    def search(
        self,
        bbox: BoundingBox,
        categories: list[str],
        limit: int,
        timeout_seconds: float,
    ) -> list[RawListing]:
        """Research real candidates. Raises SourceError/SourceTimeout."""
        ...  # pragma: no cover - interface


SOURCES: dict[str, DiscoverySource] = {}


def register_source(source: DiscoverySource) -> DiscoverySource:
    """Register a provider (called by each source module on import)."""
    SOURCES[source.name] = source
    return source


def get_source(name: str) -> DiscoverySource:
    """Return a registered provider or raise UnknownSourceError."""
    try:
        return SOURCES[name]
    except KeyError as exc:
        raise UnknownSourceError(f"unknown_source: {name}") from exc
