from __future__ import annotations

import inspect
from typing import Any

from hare.models import Model
from tests.contracts.constants import (
    EXCEPTION_ARGUMENT_VALUES,
    EXCEPTION_BASES_ANSWERED_WITH_MESSAGE,
    EXCEPTION_SECRET,
    EXCEPTION_STATUS_CODES,
    INTEGRATION_STATUS_CODES,
    SERVER_ERROR_STATUS,
)
from tests.contracts.hare_registry import HareRegistry


class HareExceptions:
    """The exceptions hare raises, found by walking the subclasses of ``Exception`` in the modules the
    environment imports - so an exception added to hare is enumerated at once."""

    @staticmethod
    def get_classes() -> list[type[Exception]]:
        """Every public exception class defined in hare.

        Returns:
            The classes, by their dotted path.
        """
        HareRegistry.get_installed_modules()
        found: dict[str, type[Exception]] = {}
        pending: list[type[Exception]] = [Exception]
        while pending:
            exception_class = pending.pop()
            pending.extend(exception_class.__subclasses__())
            if exception_class.__module__.startswith("hare.") and not exception_class.__name__.startswith("_"):
                found[HareRegistry.get_path(exception_class)] = exception_class
        return [found[path] for path in sorted(found)]

    @staticmethod
    def get_expected_status(exception_class: type[Exception], integration: str) -> int:
        """The status an integration answers an exception with - that of its nearest base the integrations
        name, the framework's 500 for an exception under none.

        Args:
            exception_class: The class.
            integration: The integration.

        Returns:
            The status.
        """
        statuses = {**EXCEPTION_STATUS_CODES, **INTEGRATION_STATUS_CODES.get(integration, {})}
        for base in exception_class.__mro__:
            status = statuses.get(HareRegistry.get_path(base))
            if status is not None:
                return status
        return SERVER_ERROR_STATUS

    @staticmethod
    def answers_with_message(exception_class: type[Exception]) -> bool:
        """Whether an integration puts the exception's message in its answer.

        Args:
            exception_class: The class.

        Returns:
            True for a refused value or a forbidden parameter.
        """
        return any(
            HareRegistry.get_path(base) in EXCEPTION_BASES_ANSWERED_WITH_MESSAGE for base in exception_class.__mro__
        )

    @staticmethod
    def make(exception_class: type[Exception]) -> Exception:
        """An instance whose message holds ``EXCEPTION_SECRET`` - what a database's message holds.

        Args:
            exception_class: The class.

        Returns:
            The instance.
        """
        arguments: list[Any] = []
        for parameter in list(inspect.signature(exception_class.__init__).parameters.values())[1:]:
            if parameter.name in {"args", "kwargs"} or parameter.default is not parameter.empty:
                continue
            if parameter.name == "model":
                arguments.append(Model)
            else:
                arguments.append(EXCEPTION_ARGUMENT_VALUES.get(parameter.name, EXCEPTION_SECRET))
        return exception_class(*arguments)
