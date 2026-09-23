"""Job board source adapters. Each exposes `async def fetch(client, company)`."""

from . import ashby, greenhouse, lever, recruitee, smartrecruiters, workable

ADAPTERS = {
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
    "workable": workable,
    "smartrecruiters": smartrecruiters,
    "recruitee": recruitee,
}

__all__ = ["ADAPTERS", "greenhouse", "lever", "ashby", "workable", "smartrecruiters", "recruitee"]
