from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any

import pytest

from hare.models import Model


@dataclasses.dataclass(frozen=True)
class ContribModelCase:
    """A model of hare's contrib packages, or a mechanism of its models, as an application declares it - what
    the contract tests carry through migrations.

    Attributes:
        name: The case's name - the test's id.
        make_models: Makes every model of the case, anew for each call.
        requires: The extras the models need.
        needs_constraints: Whether the models declare keys, uniqueness or foreign keys the database must keep.
    """

    name: str
    make_models: Callable[[], list[type[Model]]]
    requires: tuple[str, ...] = ()
    needs_constraints: bool = True

    def get_parameter(self) -> Any:
        """The case as a test's parameter, requiring its extras.

        Returns:
            The parameter, named after the case.
        """
        marks = [pytest.mark.requires(*self.requires)] if self.requires else []
        return pytest.param(self, id=self.name, marks=marks)
