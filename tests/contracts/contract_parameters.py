from __future__ import annotations

from typing import Any

import pytest


class ContractParameters:
    """Build named pytest cases with shared optional-dependency marks."""

    @staticmethod
    def make(value: Any, name: str, requires: tuple[str, ...], *marks: Any) -> Any:
        requirements = [pytest.mark.requires(*requires)] if requires else []
        return pytest.param(value, id=name, marks=[*requirements, *marks])
