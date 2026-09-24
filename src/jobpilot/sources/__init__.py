"""Job board source adapters. Each exposes `async def fetch(client, company)`."""

from . import amazon, ashby, eightfold, greenhouse, lever, oracle, recruitee, smartrecruiters, workable, workday

ADAPTERS = {
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
    "workable": workable,
    "smartrecruiters": smartrecruiters,
    "recruitee": recruitee,
    # Large-employer career sites (searched by keyword, applied to by hand).
    "workday": workday,
    "amazon": amazon,
    "eightfold": eightfold,
    "oracle": oracle,
}

__all__ = ["ADAPTERS", "greenhouse", "lever", "ashby", "workable", "smartrecruiters", "recruitee",
           "workday", "amazon", "eightfold", "oracle"]
