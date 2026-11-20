from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any

from hare.models import Model
from tests.contracts.contract_parameters import ContractParameters


@dataclasses.dataclass(frozen=True)
class ContribModelCase:
    """A model of hare's contrib packages, or a mechanism of its models, as an application declares it - what
    the contract tests carry through migrations.

    Attributes:
        name: The case's name - the test's id.
        make_models: Makes every model of the case, anew for each call.
        requires: The extras the models need.
        dialects: Expected supporting profiles, independent of the models' field implementation.
    """

    name: str
    make_models: Callable[[], list[type[Model]]]
    requires: tuple[str, ...] = ()
    dialects: tuple[str, ...] = ("sqlite", "postgresql")

    def get_parameter(self) -> Any:
        """The case as a test's parameter, requiring its extras.

        Returns:
            The parameter, named after the case.
        """
        return ContractParameters.make(self, self.name, self.requires)
