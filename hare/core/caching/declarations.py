from __future__ import annotations

from typing import Any


class SharedCacheBucket(dict[Any, Any]):
    """A dict ``Cache.new_shared_bucket()`` hands out - a subclass only so the cache can refer to
    it weakly."""
