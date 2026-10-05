"""The migration safety check's rules every database shares, and the dialect part listing a
dialect's rules."""

from __future__ import annotations

from hare.dialects.base.migration_safety.migration_safety_rule import MigrationSafetyRule
from hare.dialects.base.migration_safety.migration_safety_rules import MigrationSafetyRules

__all__ = [
    "MigrationSafetyRule",
    "MigrationSafetyRules",
]
