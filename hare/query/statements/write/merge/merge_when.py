from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from hare.classes.value_descriptions import ValueDescriptions
from hare.dialects.base.clauses.enums import MergeAction, MergeMatch
from hare.exceptions import QueryError
from hare.query.expressions.conditions.q import Q
from hare.query.filters.resolution.filter_values import FilterValues


@dataclass(frozen=True, slots=True)
class MergeWhen:
    """One ``when_...()`` branch of ``merge()`` - the rows it takes, what it does with them, the values
    it writes and its condition.

    Attributes:
        match: Which rows it takes.
        action: What it does with them.
        values: The written value of each field - an update's or an insert's.
        condition: The condition the branch takes its rows under, None for every row.
    """

    match: MergeMatch
    action: MergeAction
    values: Mapping[str, Any] = field(default_factory=dict)
    condition: Q | None = None

    @classmethod
    def build(
        cls,
        method_name: str,
        match: MergeMatch,
        actions: Mapping[MergeAction, Any],
        condition: Any,
    ) -> MergeWhen:
        """The branch of a ``when_...()`` call.

        Args:
            method_name: The call, named in the errors.
            match: Which rows it takes.
            actions: The value of each action argument of the call - a mapping of values for an
                update or insert, a bool for a delete or ``do_nothing``.
            condition: A ``Q`` or None.

        Returns:
            The branch.

        Raises:
            QueryError: Not exactly one action is given, a value mapping is empty or not keyed by
                field names, or the condition isn't a ``Q``.
        """
        chosen: list[MergeAction] = []
        for action, argument in actions.items():
            if action in {MergeAction.UPDATE, MergeAction.INSERT}:
                if argument is None:
                    continue
                if (
                    not isinstance(argument, Mapping)
                    or not argument
                    or not all(isinstance(name, str) for name in argument)
                ):
                    raise QueryError(
                        f"{method_name}({action}=...) takes a dict of field values, got "
                        f"{ValueDescriptions.get_shown_value(argument)}"
                    )
                chosen.append(action)
            else:
                if argument:
                    chosen.append(action)
        if len(chosen) != 1:
            names = ", ".join(str(action) for action in actions)
            raise QueryError(f"{method_name}() takes one of {names}, got {len(chosen)}")
        if condition is not None and not isinstance(condition, Q):
            raise QueryError(
                f"{method_name}(condition=...) takes a Q, got {ValueDescriptions.get_shown_value(condition)}"
            )
        action = chosen[0]
        # The values as they are at the call - changing them before the merge runs changes no merge.
        values = (
            {name: FilterValues.get_detached_value(value, written=True) for name, value in actions[action].items()}
            if action in {MergeAction.UPDATE, MergeAction.INSERT}
            else {}
        )
        return cls(match, action, values, condition)
