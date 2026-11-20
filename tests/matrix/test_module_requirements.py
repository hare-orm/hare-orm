from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TestModuleRequirements:
    """Import requirements and optional requirements of a module's test cells."""

    __test__ = False

    requires: frozenset[str]
    cells: frozenset[str]
