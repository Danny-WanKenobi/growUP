"""Discovery source integrations (one modular provider per module).

Providers register themselves here on import so the registry always
reflects the available sources.
"""

from app.agents.gabriel.sources import osm_api as osm_api  # noqa: F401
from app.agents.gabriel.sources import overpass as overpass  # noqa: F401
