"""Application prefill (stop before submit) and approval-gated submission."""

from . import ashby, greenhouse, lever

# HN jobs have no fillable form; they go to the manual-apply queue instead.
FILLERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby}

__all__ = ["FILLERS"]
