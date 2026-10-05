"""Append-only versioned models: every change a new row of the same ``id`` and a higher ``version``."""

from __future__ import annotations

from hare.contrib.versioning.versioned_model import VersionedModel

__all__ = ["VersionedModel"]
