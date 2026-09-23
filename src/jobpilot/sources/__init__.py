"""Job board source adapters. Each exposes `async def fetch(client, company)`."""

from . import ashby, greenhouse, lever

ADAPTERS = {
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
}

__all__ = ["ADAPTERS", "greenhouse", "lever", "ashby"]
