"""Gabriel discovery endpoints (minimal, bounded, testable)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status

from app.agents.gabriel.schemas import (
    DiscoveryRunRequest,
    DiscoveryRunResult,
    DiscoverySourceInfo,
)
from app.agents.gabriel.service import run_discovery
from app.agents.gabriel.sources.base import (
    SOURCES,
    SourceError,
    SourceTimeout,
    UnknownSourceError,
)
from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["discovery"])


@router.get("/discovery/sources", response_model=list[DiscoverySourceInfo])
def list_sources() -> list:
    """List registered discovery providers and their requirements."""
    return [
        {
            "name": source.name,
            "description": source.description,
            "docs_url": source.docs_url,
            "requires_credentials": source.requires_credentials,
        }
        for source in SOURCES.values()
    ]


@router.post("/discovery/runs", response_model=DiscoveryRunResult)
def start_discovery_run(body: DiscoveryRunRequest) -> DiscoveryRunResult:
    """Run one bounded discovery pass and persist new candidates.

    Unknown source names are rejected; provider failures return 502
    with nothing fabricated or persisted.
    """
    try:
        return run_discovery(body)
    except DatabaseNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="database_not_configured"
        ) from exc
    except DatabaseUnavailableError as exc:
        logger.warning("discovery run database failure: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="database_unavailable"
        ) from exc
    except UnknownSourceError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="unknown_source"
        ) from exc
    except (SourceTimeout, SourceError) as exc:
        logger.warning("discovery run source failure: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="source_unavailable"
        ) from exc
