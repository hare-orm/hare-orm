from __future__ import annotations

import contextlib
from abc import ABC, abstractmethod
from collections.abc import Generator
from typing import TYPE_CHECKING, Any, ClassVar

from hare.models import Model
from tests.contracts.writes.enums import WriteOutcome, WriteRole

if TYPE_CHECKING:
    from tests.contracts.writes.write_path import WritePath
    from tests.contracts.writes.write_scenario import WriteScenario


class WriteObserver(ABC):
    """Something that watches the rows a write path writes - the contract matrix checks every write path
    against it. A subclass says what each path does to what it watches, and checks it.

    Attributes:
        name: The observer's name - the test's id.
        outcomes: What each write path does to what the observer watches, by the path's name.
        requires: The extras the observer's models need.
        required_features: The features of the dialect the observer's models need.
        path_required_features: The features of the database what the observer watches needs of a
            path, by the path's name.
    """

    name: ClassVar[str]
    outcomes: ClassVar[dict[str, WriteOutcome]]
    requires: ClassVar[tuple[str, ...]] = ()
    required_features: ClassVar[tuple[str, ...]] = ()
    path_required_features: ClassVar[dict[str, tuple[str, ...]]] = {}

    def get_field_attributes(self, role: WriteRole) -> dict[str, Any]:
        """The fields the observer adds to a model of the matrix.

        Args:
            role: The model.

        Returns:
            The fields by name - made anew for each call.
        """
        return {}

    def get_meta_options(self, role: WriteRole) -> dict[str, Any]:
        """The ``Meta`` options the observer adds to a model of the matrix.

        Args:
            role: The model.

        Returns:
            The options by name.
        """
        return {}

    def get_amount_value(self, amount: int) -> Any:
        """The value written to the matrix's ``amount`` field for an amount - the type of the field.

        Args:
            amount: The amount.

        Returns:
            The value.
        """
        return amount

    def get_extra_models(self) -> tuple[type[Model], ...]:
        """The models the observer needs besides those of the matrix.

        Returns:
            The models.
        """
        return ()

    @contextlib.contextmanager
    def get_scope(self) -> Generator[None]:
        """What the observer wraps every write of a scenario in - the scope of a tenant.

        Yields:
            Nothing.
        """
        yield

    def get_outcome(self, path: WritePath) -> WriteOutcome:
        """What a path does to what the observer watches.

        Args:
            path: The write path.

        Returns:
            The outcome.
        """
        return self.outcomes[path.name]

    @abstractmethod
    async def check(self, scenario: WriteScenario, path: WritePath, outcome: WriteOutcome) -> None:
        """Runs a path and checks what it does to what the observer watches.

        Args:
            scenario: The scenario, its rows prepared.
            path: The write path.
            outcome: What the path is to do to what the observer watches.
        """
