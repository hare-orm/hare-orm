"""Operations on the privileges a model grants to roles."""

from __future__ import annotations

from hare.migrations.operations.grants.add_grant import AddGrant
from hare.migrations.operations.grants.remove_grant import RemoveGrant

__all__ = ["AddGrant", "RemoveGrant"]
