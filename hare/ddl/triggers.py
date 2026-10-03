from dataclasses import dataclass
from typing import Any

from hare.ddl.enums import TriggerForEach, TriggerTiming
from hare.exceptions import ConfigurationError
from hare.utils.class_path import ClassPath


@dataclass(frozen=True)
class Trigger:
    """A database trigger with its function, managed as one. On Postgres a ``CREATE FUNCTION ...
    RETURNS TRIGGER`` and the ``CREATE TRIGGER`` are made and dropped together; on SQLite ``body``
    goes into ``CREATE TRIGGER ... BEGIN ... END``.

    Args:
        name: The trigger's name - the Postgres function is ``f"{name}_fn"``.
        on: The event - a ``TriggerEvent``, or text for a combination (``"INSERT OR UPDATE"``).
        body: The procedural body in full (Postgres: ending with ``RETURN NEW;`` or the like). Not
            portable - a project on both dialects declares a trigger for each.
        timing: ``TriggerTiming.BEFORE``, ``AFTER`` or (PostgreSQL, on views) ``INSTEAD_OF``.
        for_each: ``TriggerForEach.ROW`` or ``STATEMENT`` - the latter needs
            ``Dialect.supports_statement_triggers``.
        when: A raw SQL ``WHEN`` condition - not portable either.
        language: The function's procedural language; None for the dialect's default.
        deferrable: Makes it a ``CREATE CONSTRAINT TRIGGER``, deferrable to transaction end - needs
            ``Dialect.supports_deferrable_constraints``; always ``AFTER``, ``FOR EACH ROW``.
        initially_deferred: Only with ``deferrable=True`` - every transaction starts deferred.

    Raises:
        ConfigurationError: ``timing`` or ``for_each`` is invalid, ``initially_deferred=True``
            without ``deferrable=True``, or ``deferrable=True`` with a timing or level a constraint
            trigger can't have.
    """

    name: str
    on: str
    body: str
    timing: TriggerTiming = TriggerTiming.AFTER
    for_each: TriggerForEach = TriggerForEach.ROW
    when: str | None = None
    language: str | None = None
    deferrable: bool = False
    initially_deferred: bool = False

    def __post_init__(self) -> None:
        if self.timing not in set(TriggerTiming):
            raise ConfigurationError(f"Trigger.timing must be one of {', '.join(TriggerTiming)}, got {self.timing!r}")
        if self.for_each not in set(TriggerForEach):
            raise ConfigurationError(
                f"Trigger.for_each must be one of {', '.join(TriggerForEach)}, got {self.for_each!r}"
            )
        if self.initially_deferred and not self.deferrable:
            raise ConfigurationError("Trigger.initially_deferred requires deferrable=True")
        if self.deferrable:
            if self.timing != TriggerTiming.AFTER:
                raise ConfigurationError(
                    f"Trigger.timing must be TriggerTiming.AFTER when deferrable=True - a deferred "
                    f"trigger fires after the statement, got {self.timing!r}"
                )
            if self.for_each != TriggerForEach.ROW:
                raise ConfigurationError(
                    f"Trigger.for_each must be TriggerForEach.ROW when deferrable=True - a deferred "
                    f"trigger fires for each row, got {self.for_each!r}"
                )

    @property
    def function_name(self) -> str:
        """The name of the backing function this trigger's body runs in, on a dialect that has one."""
        return f"{self.name}_fn"

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        path = ClassPath.get(self.__class__)
        kwargs: dict[str, Any] = {"name": self.name, "on": self.on, "body": self.body}
        if self.timing != TriggerTiming.AFTER:
            kwargs["timing"] = TriggerTiming(self.timing)
        if self.for_each != TriggerForEach.ROW:
            kwargs["for_each"] = TriggerForEach(self.for_each)
        if self.when is not None:
            kwargs["when"] = self.when
        if self.language is not None:
            kwargs["language"] = self.language
        if self.deferrable:
            kwargs["deferrable"] = self.deferrable
        if self.initially_deferred:
            kwargs["initially_deferred"] = self.initially_deferred
        return path, [], kwargs
