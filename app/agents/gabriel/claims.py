"""Validated county-claim construction (B5-2A step 2A.2a).

The sole approved constructor for ordinary listing/snippet-derived
county evidence. It preserves provenance (original mention, source URL,
canonical host, excerpt, observation metadata) while granting nothing:
every claim it builds carries the default non-authoritative authority,
an empty property match, and no syndication identifier.

There is intentionally no parameter for authority, property matching,
syndication identity, or source domain. Source identity always derives
from the source URL via the accepted source-identity utilities, so a
caller cannot smuggle an override through construction.

A future official-record adapter will build verified-capable evidence
through a separate, explicitly authorized path. It does not exist yet,
and nothing here unlocks verification.
"""

from __future__ import annotations

from app.agents.gabriel.attribution import CountyClaim, normalize_county_claim
from app.agents.gabriel.source_identity import canonical_hostname


def build_county_claim(
    *,
    mention: str,
    source_url: str,
    excerpt: str = "",
    observed_at: str | None = None,
    round_no: int = 0,
) -> CountyClaim:
    """Build one non-authoritative county claim from listing evidence.

    The mention is preserved verbatim and normalized with the shared
    county normalizer (malformed mentions keep ``normalized=None``).
    The source domain always derives from the source URL; no caller
    label can substitute for it. Authority stays at the model default
    (``listing``), the property match stays empty, and no syndication
    identifier is assigned: cross-host syndication grouping is
    explicitly out of scope for this checkpoint.
    """
    return CountyClaim(
        original=mention,
        normalized=normalize_county_claim(mention),
        source_url=source_url,
        source_domain=canonical_hostname(source_url),
        excerpt=excerpt,
        observed_at=observed_at,
        round_no=round_no,
    )
