"""Brave Search Web API research provider (MVP-004B-2).

Executes planner assignments against the official Brave Search Web API
(docs: https://api-dashboard.search.brave.com/app/documentation/web-search/get-started).
Authentication: ``X-Subscription-Token`` header with the key from the
``BRAVE_SEARCH_API_KEY`` environment variable. Credentials never appear
in logs, errors, responses, or fixtures.

Results are normalized into provider-independent typed models. Missing
fields stay unknown; nothing is inferred from result text. Failures
raise typed errors; fictional results are never substituted.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from urllib.parse import urlparse

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
BRAVE_DOCS_URL = "https://api-dashboard.search.brave.com/app/documentation/web-search/get-started"
BRAVE_KEY_ENV_VAR = "BRAVE_SEARCH_API_KEY"
BRAVE_RESULT_COUNT = 10

PROVIDER_NAME = "brave"


class BraveConfigError(ValueError):
    """Brave credentials are missing or invalid (fail closed)."""


class BraveError(Exception):
    """A Brave request failed (rate limit, HTTP error, bad payload)."""


class BraveTimeout(BraveError):
    """Brave did not answer within the time budget."""


class BraveRateLimited(BraveError):
    """Brave returned HTTP 429 (caller must back off, not retry blindly)."""


class BraveUnauthorized(BraveError):
    """Brave rejected the subscription token (abort the run)."""


class BraveSearchResult(BaseModel):
    """One normalized, attributable web search result."""

    assignment_id: str
    query: str
    title: str = ""
    url: str = ""
    snippet: str = ""
    domain: str = ""
    provider: str = PROVIDER_NAME
    retrieved_at: str
    published_at: str | None = None
    rank: int = Field(ge=1)


def get_brave_api_key() -> str:
    """Read the Brave key; raises BraveConfigError when unusable."""
    key = os.getenv(BRAVE_KEY_ENV_VAR, "")
    if not key or not key.strip():
        raise BraveConfigError("Brave API key is not configured")
    return key


def _domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc
    except (ValueError, TypeError):
        return ""


def parse_results(
    payload: object, *, assignment_id: str, query: str, retrieved_at: str
) -> list[BraveSearchResult]:
    """Normalize a Brave web-search payload (no inference, no guessing)."""
    if not isinstance(payload, dict) or not isinstance(payload.get("web"), dict):
        raise BraveError("brave_unexpected_response")
    raw_results = payload["web"].get("results", [])
    if not isinstance(raw_results, list):
        raise BraveError("brave_unexpected_response")
    results: list[BraveSearchResult] = []
    for rank, item in enumerate(raw_results, start=1):
        if not isinstance(item, dict):
            continue
        url = str(item.get("url", "") or "")
        if not url:
            continue  # unattributable entries are dropped, never invented around
        results.append(
            BraveSearchResult(
                assignment_id=assignment_id,
                query=query,
                title=str(item.get("title", "") or ""),
                url=url,
                snippet=str(item.get("description", "") or ""),
                domain=_domain_of(url),
                retrieved_at=retrieved_at,
                published_at=str(item["age"]) if item.get("age") else None,
                rank=rank,
            )
        )
    return results


def execute_query(query: str, assignment_id: str, timeout_seconds: float) -> list[BraveSearchResult]:
    """Run one Brave web search and return normalized results."""
    import httpx

    api_key = get_brave_api_key()
    retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        response = httpx.get(
            BRAVE_SEARCH_URL,
            params={"q": query, "count": BRAVE_RESULT_COUNT, "country": "US", "search_lang": "en"},
            headers={"X-Subscription-Token": api_key, "Accept": "application/json"},
            timeout=timeout_seconds,
        )
    except httpx.TimeoutException as exc:
        raise BraveTimeout(f"brave_timeout after {timeout_seconds}s") from exc
    except httpx.HTTPError as exc:
        raise BraveError("brave_unreachable") from exc
    if response.status_code == 429:
        logger.warning("brave rate limit hit")
        raise BraveRateLimited("brave_rate_limited")
    if response.status_code in (401, 403):
        logger.warning("brave rejected the subscription token")
        raise BraveUnauthorized("brave_unauthorized")
    if response.status_code != 200:
        logger.warning("brave query failed: HTTP %s", response.status_code)
        raise BraveError("brave_bad_response")
    try:
        payload = response.json()
    except ValueError as exc:
        raise BraveError("brave_invalid_json") from exc
    return parse_results(payload, assignment_id=assignment_id, query=query, retrieved_at=retrieved_at)


class BraveResearchProvider:
    """Assignment-based Brave research (separate from geo discovery sources)."""

    name = PROVIDER_NAME
    description = (
        "Brave Search Web API results for planner assignments (titles, URLs, "
        "snippets only; no valuation or distress inference)."
    )
    docs_url = BRAVE_DOCS_URL
    requires_credentials = True

    def execute(self, query: str, assignment_id: str, timeout_seconds: float) -> list[BraveSearchResult]:
        return execute_query(query, assignment_id, timeout_seconds)
