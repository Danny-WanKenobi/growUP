"""Vetted source registry for the explainable verification policy.

Maps canonical hostnames to source-credibility classes. Classification
establishes voice *eligibility* only -- never claim truth. Unknown hosts
fail closed to UNVETTED. Nothing here is inferred: every entry is an
explicit curation decision with provenance metadata.

The production registry starts EMPTY. Entries are added only through
separately reviewed curation; tests build synthetic registries in memory.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, field_validator

from app.agents.gabriel.source_identity import canonical_host_text


class SourceClass(str, Enum):
    """Credibility class of a publication outlet (eligibility, not truth)."""

    AUTHORITATIVE = "authoritative"
    VETTED_COMMERCIAL = "vetted_commercial"
    SECONDARY = "secondary"
    UNVETTED = "unvetted"


class RegistryEntry(BaseModel):
    """One curated hostname classification."""

    hostname: str  # must already be canonical
    source_class: SourceClass
    outlet_name: str = ""
    publisher: str = ""  # publisher identity used for independence grouping
    record_types: list[str] = Field(default_factory=list)  # authoritative: eligible record kinds
    listed_by_required: bool = False  # marketplace hosts: listing must name its originator
    added_by: str = ""
    added_on: str = ""
    review_due: str = ""
    note: str = ""

    @field_validator("hostname")
    @classmethod
    def _must_be_canonical(cls, value: str) -> str:
        canonical = canonical_host_text(value)
        if not canonical or canonical != value:
            raise ValueError("registry hostname must be canonical")
        return value


class SourceRegistry(BaseModel):
    """Explicit hostname → class map. Unknown hosts are UNVETTED."""

    entries: dict[str, RegistryEntry] = Field(default_factory=dict)

    def lookup(self, url_or_host: str) -> RegistryEntry | None:
        """Return the entry for a URL or host string, or None when unknown."""
        return self.entries.get(canonical_host_text(url_or_host or ""))

    def source_class_of(self, url_or_host: str) -> SourceClass:
        """Credibility class for a URL or host; unknown fails closed."""
        entry = self.lookup(url_or_host)
        return entry.source_class if entry is not None else SourceClass.UNVETTED

    def publisher_of(self, url_or_host: str) -> str:
        """Registry publisher identity, or "" when unknown."""
        entry = self.lookup(url_or_host)
        return entry.publisher if entry is not None else ""


# Production registry: zero vetted entries until separately reviewed curation.
PRODUCTION_REGISTRY = SourceRegistry()
