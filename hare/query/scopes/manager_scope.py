from __future__ import annotations

from hare.exceptions import ConfigurationError
from hare.query.expressions import Q
from hare.query.scopes.row_scope import RowScope
from hare.query.scopes.row_visibility import RowVisibility


class ManagerScope(RowScope):
    """A ``Meta.manager`` overriding ``get_queryset()``: the filters of the queryset it returns."""

    __slots__ = ()

    def get_condition(self, visibility: RowVisibility) -> Q | None:
        """
        Raises:
            ConfigurationError: If the manager's queryset carries state with no condition
                equivalent (annotations, grouping, ``LIMIT``/``OFFSET``, ``DISTINCT``, CTEs).
        """
        model = self.model
        # Deferred import: RowScopes builds this scope.
        from hare.query.scopes.row_scopes import RowScopes

        queryset = RowScopes.get_queryset(model, visibility)
        unsupported = [
            name
            for name, is_present in (
                ("annotate()/alias()", bool(queryset._annotations)),
                ("group_by()", bool(queryset._group_bys)),
                ("limit()/offset()", queryset._limit is not None or queryset._offset is not None),
                ("distinct()", queryset._distinct or bool(queryset._distinct_on)),
                ("with_cte()", bool(queryset._with_ctes)),
            )
            if is_present
        ]
        if unsupported:
            raise ConfigurationError(
                f"{model.__name__}'s Meta.manager get_queryset() applies {', '.join(unsupported)}, "
                "which can't be expressed as a JOIN's ON condition - a relation-crossing query "
                f"(select_related()/.only()/.order_by()/a nested filter) to {model.__name__} needs "
                "its default scope to be plain .filter()/.exclude() conditions only"
            )
        if queryset._is_none:
            return Q(pk__in=[])
        if not queryset._q_objects:
            return None
        condition = queryset._q_objects[0]
        for q_object in queryset._q_objects[1:]:
            condition = condition & q_object
        return condition
