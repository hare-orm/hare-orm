from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.classes.class_path import ClassPath
from hare.classes.value_descriptions import ValueDescriptions
from hare.ddl.constants import TRIGGER_NEW_ROW, TRIGGER_OLD_ROW, TRIGGER_OLD_ROW_EVENTS
from hare.ddl.enums import TriggerForEach, TriggerTiming
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from hare.query.expressions import Q


@dataclass(frozen=True)
class Trigger:
    """A database trigger with its function, managed as one: a dialect whose triggers call a function
    makes and drops the function and the ``CREATE TRIGGER`` together; another writes ``body`` into the
    trigger itself.

    Args:
        name: The trigger's name - a function the dialect makes for it is ``f"{name}_fn"``.
        on: The event - a ``TriggerEvent``, or text for a combination (``"INSERT OR UPDATE"``).
        body: ``RawSQLTerm`` of the procedural body in full, as the dialect's procedural language
            writes it (``RETURN NEW;`` ending a function body, say). Not portable - a project on
            several dialects declares a trigger for each.
        timing: ``TriggerTiming.BEFORE``, ``AFTER`` or ``INSTEAD_OF`` (on views, where the dialect's
            triggers take it).
        for_each: ``TriggerForEach.ROW`` or ``STATEMENT`` - the latter refused where a trigger fires
            for every row.
        when: The ``WHEN`` condition - a ``Q`` over the model's own fields, compared on the row the
            trigger fires for (``NEW``; ``OLD`` for a ``DELETE`` trigger), or ``RawSQLTerm`` of a raw
            SQL condition.
        language: The function's procedural language; None for the dialect's default.
        deferrable: Makes it a constraint trigger, deferrable to transaction end - needs
            ``Features.supports_deferrable_constraints``; always ``AFTER``, ``FOR EACH ROW``.
        initially_deferred: Only with ``deferrable=True`` - every transaction starts deferred.
        enabled: False for a trigger kept but not fired (``ALTER TABLE ... DISABLE TRIGGER``) - a
            dialect firing every trigger it has refuses it.

    Raises:
        ConfigurationError: ``body`` isn't a ``RawSQLTerm``, ``when`` is neither a ``Q`` nor a
            ``RawSQLTerm``, a ``Q`` condition is given to a ``STATEMENT`` trigger or to one firing
            on ``DELETE`` together with another event, ``timing`` or ``for_each`` is invalid,
            ``initially_deferred=True`` without ``deferrable=True``, or ``deferrable=True`` with a
            timing or level a constraint trigger can't have.
    """

    name: str
    on: str
    body: RawSQLTerm
    timing: TriggerTiming = TriggerTiming.AFTER
    for_each: TriggerForEach = TriggerForEach.ROW
    when: Q | RawSQLTerm | None = None
    language: str | None = None
    deferrable: bool = False
    initially_deferred: bool = False
    enabled: bool = True

    def __post_init__(self) -> None:
        # Local import: the query package imports the ddl package.
        from hare.query.expressions import Q

        if not isinstance(self.body, RawSQLTerm):
            raise ConfigurationError(
                f"Trigger.body takes RawSQLTerm(...) of raw SQL, got {ValueDescriptions.get_shown_value(self.body)}"
            )
        if self.when is not None and not isinstance(self.when, (Q, RawSQLTerm)):
            raise ConfigurationError(
                f"Trigger.when takes a Q over the model's fields or RawSQLTerm(...) of raw SQL, got "
                f"{ValueDescriptions.get_shown_value(self.when)}"
            )
        if not isinstance(self.timing, str) or self.timing not in set(TriggerTiming):
            raise ConfigurationError(f"Trigger.timing must be one of {', '.join(TriggerTiming)}, got {self.timing!r}")
        if not isinstance(self.for_each, str) or self.for_each not in set(TriggerForEach):
            raise ConfigurationError(
                f"Trigger.for_each must be one of {', '.join(TriggerForEach)}, got {self.for_each!r}"
            )
        if isinstance(self.when, Q):
            if self.for_each == TriggerForEach.STATEMENT:
                raise ConfigurationError(
                    "Trigger.when can't be a Q of a STATEMENT trigger - it has no row to compare; give "
                    "RawSQLTerm(...) of raw SQL"
                )
            events = self.get_events()
            if events & TRIGGER_OLD_ROW_EVENTS and events - TRIGGER_OLD_ROW_EVENTS:
                raise ConfigurationError(
                    f"Trigger.when can't be a Q of a trigger on {self.on!r} - a DELETE reads the old row, "
                    "another event the new one; give RawSQLTerm(...) of raw SQL"
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

    def get_events(self) -> frozenset[str]:
        """The events the trigger fires on - ``UPDATE OF a, b`` as ``UPDATE``.

        Returns:
            The events' names, upper-case.
        """
        return frozenset(event.strip().split()[0].upper() for event in str(self.on).upper().split(" OR "))

    def get_condition_row(self) -> str:
        """The row a ``Q`` condition reads - ``OLD`` of a ``DELETE`` trigger, ``NEW`` of any other.

        Returns:
            The row's keyword.
        """
        return TRIGGER_OLD_ROW if self.get_events() <= TRIGGER_OLD_ROW_EVENTS else TRIGGER_NEW_ROW

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        """The declaration as a migration writes it.

        Returns:
            The import path of its class, its positional arguments and the keyword arguments it was given
            other than by default.
        """
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
        if not self.enabled:
            kwargs["enabled"] = self.enabled
        return path, [], kwargs
