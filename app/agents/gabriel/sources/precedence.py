"""Gabriel discovery-source precedence contract (owner-directed).

Every new geographic jurisdiction is discovered in this order:

1. **Official government data** — appraisal/assessor districts,
   parcel and GIS datasets, local open-data portals,
   tax/property records, zoning, and other authoritative public
   datasets (direct official downloads, published APIs, legally
   permitted bulk data; no paid APIs, paywalls, unauthorized
   scraping, or bypassed access restrictions).
2. **SearXNG** — an approved instance for supplemental general
   web discovery, commercial-property listings, and evidence
   enrichment, integrated into the existing discovery engine.
3. **Brave Search API** — the existing integration, demoted to
   third-priority fallback. No automatic paid usage without
   standing budget authorization; free discovery is exhausted
   first.

Qwen is excluded from Gabriel entirely (reserved for Wondrue
and Uriel); any declaration naming it is refused.

This module is the configuration contract only: it validates a
declaration and answers ordering questions. Registering a tier
authorizes no live collection, spending, or provider
integration, and changes no existing discovery behavior. The
shipped declaration is
``config/discovery_sources.example.json``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

__all__ = [
    "EXCLUDED_FROM_GABRIEL",
    "PrecedenceContract",
    "PrecedenceError",
    "REQUIRED_TIER_IDS",
    "load_precedence_contract",
    "parse_precedence_contract",
    "precedence_report",
]

#: Tier ids in required owner-directed order (index + 1 == tier).
REQUIRED_TIER_IDS = ("official_government", "searxng", "brave")

#: Model families that must never be routed inside Gabriel.
EXCLUDED_FROM_GABRIEL = ("qwen",)

_TIER_KEYS = frozenset({"tier", "id", "sources"})
_SOURCE_KEYS = frozenset(
    {
        "name",
        "kind",
        "status",
        "requires_credentials",
        "requires_budget_authorization",
        "notes",
    }
)
_TOP_KEYS = frozenset(
    {
        "contract",
        "description",
        "tiers",
        "existing_geographic_index",
        "official_evidence_adapters",
        "excluded_from_gabriel",
    }
)
_STATUSES = frozenset(
    {"existing", "existing_fallback", "planned", "catalog_required"}
)


class PrecedenceError(ValueError):
    """A discovery-source declaration violates the precedence contract."""


@dataclass(frozen=True)
class PrecedenceSource:
    """One declared discovery source within a tier."""

    name: str
    kind: str
    status: str
    requires_credentials: bool = False
    requires_budget_authorization: bool = False


@dataclass(frozen=True)
class PrecedenceContract:
    """A validated discovery-source precedence declaration."""

    tiers: tuple[tuple[PrecedenceSource, ...], ...]
    geographic_index: tuple[PrecedenceSource, ...] = ()
    evidence_adapters: tuple[PrecedenceSource, ...] = ()

    def tier_of(self, source_name: str) -> int | None:
        """1-based precedence tier for a discovery source (None if undeclared)."""
        for index, sources in enumerate(self.tiers):
            if any(source.name == source_name for source in sources):
                return index + 1
        return None

    def source(self, source_name: str) -> PrecedenceSource | None:
        """The declared tier source by name (None if undeclared)."""
        for sources in self.tiers:
            for source in sources:
                if source.name == source_name:
                    return source
        return None

    def ordered_tier_sources(self) -> tuple[PrecedenceSource, ...]:
        """All tier sources flattened in precedence order."""
        return tuple(source for sources in self.tiers for source in sources)


def _parse_source(raw: Any, where: str) -> PrecedenceSource:
    if not isinstance(raw, dict):
        raise PrecedenceError(f"{where} must be an object")
    for key in raw:
        if key not in _SOURCE_KEYS:
            raise PrecedenceError(f"unknown {where} key: {key!r}")
    name = str(raw.get("name", "") or "")
    if not name:
        raise PrecedenceError(f"{where} needs a name")
    if any(token in name.lower() for token in EXCLUDED_FROM_GABRIEL):
        raise PrecedenceError(f"{name!r} is excluded from Gabriel")
    status = str(raw.get("status", "") or "")
    if status not in _STATUSES:
        raise PrecedenceError(f"{where} has unsupported status: {status!r}")
    return PrecedenceSource(
        name=name,
        kind=str(raw.get("kind", "") or ""),
        status=status,
        requires_credentials=bool(raw.get("requires_credentials", False)),
        requires_budget_authorization=bool(
            raw.get("requires_budget_authorization", False)
        ),
    )


def parse_precedence_contract(raw: Any) -> PrecedenceContract:
    """Validate one precedence declaration (fail closed, no partial trust)."""
    if not isinstance(raw, dict):
        raise PrecedenceError("precedence declaration must be one JSON object")
    for key in raw:
        if key not in _TOP_KEYS:
            raise PrecedenceError(f"unknown precedence key: {key!r}")
    excluded = raw.get("excluded_from_gabriel")
    if not isinstance(excluded, list) or not all(
        isinstance(item, str) for item in excluded
    ):
        raise PrecedenceError("excluded_from_gabriel must be a list of names")
    for required in EXCLUDED_FROM_GABRIEL:
        if required not in excluded:
            raise PrecedenceError(
                f"excluded_from_gabriel must keep {required!r} excluded"
            )
    tiers_raw = raw.get("tiers")
    if not isinstance(tiers_raw, list) or len(tiers_raw) != len(REQUIRED_TIER_IDS):
        raise PrecedenceError(
            f"tiers must declare exactly {len(REQUIRED_TIER_IDS)} tiers"
        )
    tiers: list[tuple[PrecedenceSource, ...]] = []
    seen: set[str] = set()
    for index, tier_raw in enumerate(tiers_raw):
        if not isinstance(tier_raw, dict):
            raise PrecedenceError(f"tier {index + 1} must be an object")
        for key in tier_raw:
            if key not in _TIER_KEYS:
                raise PrecedenceError(f"unknown tier key: {key!r}")
        if tier_raw.get("tier") != index + 1 or tier_raw.get("id") != (
            REQUIRED_TIER_IDS[index]
        ):
            raise PrecedenceError(
                f"tier {index + 1} must be {REQUIRED_TIER_IDS[index]!r} "
                "in owner-directed order"
            )
        sources_raw = tier_raw.get("sources")
        if not isinstance(sources_raw, list) or not sources_raw:
            raise PrecedenceError(f"tier {index + 1} needs at least one source")
        sources = tuple(
            _parse_source(item, f"tier {index + 1} source") for item in sources_raw
        )
        for source in sources:
            if source.name in seen:
                raise PrecedenceError(f"duplicate declared source: {source.name!r}")
            seen.add(source.name)
        tiers.append(sources)
    brave = tiers[-1]
    if not any(
        source.requires_budget_authorization for source in brave
    ):
        raise PrecedenceError(
            "the Brave fallback tier must require standing budget authorization"
        )
    geographic_index = tuple(
        _parse_source(item, "geographic index source")
        for item in raw.get("existing_geographic_index") or []
    )
    evidence_adapters = tuple(
        _parse_source(item, "evidence adapter")
        for item in raw.get("official_evidence_adapters") or []
    )
    return PrecedenceContract(
        tiers=tuple(tiers),
        geographic_index=geographic_index,
        evidence_adapters=evidence_adapters,
    )


def load_precedence_contract(path: str) -> PrecedenceContract:
    """Load and validate a precedence declaration from a JSON file."""
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    return parse_precedence_contract(raw)


def precedence_report(contract: PrecedenceContract) -> dict[str, Any]:
    """Ordered, JSON-safe precedence summary for reports and docs."""
    return {
        "tiers": [
            {
                "tier": index + 1,
                "id": REQUIRED_TIER_IDS[index],
                "sources": [
                    {
                        "name": source.name,
                        "kind": source.kind,
                        "status": source.status,
                        "requires_credentials": source.requires_credentials,
                        "requires_budget_authorization": (
                            source.requires_budget_authorization
                        ),
                    }
                    for source in sources
                ],
            }
            for index, sources in enumerate(contract.tiers)
        ],
        "existing_geographic_index": [s.name for s in contract.geographic_index],
        "official_evidence_adapters": [s.name for s in contract.evidence_adapters],
        "excluded_from_gabriel": list(EXCLUDED_FROM_GABRIEL),
    }
