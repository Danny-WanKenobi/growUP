"""Grow Up FastAPI application entry point.

Deployment expects the ASGI app at ``app.main:app`` serving ``GET
/health`` on 127.0.0.1:8001. This module stays thin: routes and
database logic live in their owned modules.
"""

from __future__ import annotations

from fastapi import FastAPI

from app import __version__
from app.routes.discovery import router as discovery_router
from app.routes.health import router as health_router
from app.routes.properties import router as properties_router
from app.routes.readiness import router as readiness_router


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    application = FastAPI(
        title="Grow Up",
        version=__version__,
        description="AI-powered platform for evaluating indoor-farm conversions.",
    )
    application.include_router(discovery_router)
    application.include_router(health_router)
    application.include_router(readiness_router)
    application.include_router(properties_router)
    return application


app = create_app()
