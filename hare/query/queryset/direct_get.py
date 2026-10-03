from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.query.plans.description.plan_description import PlanDescription
    from hare.query.plans.statement_plan import StatementPlan


@dataclass(slots=True)
class DirectGet:
    """What ``get()``/``get_or_none()`` of a queryset nothing else changed runs on - the statement
    plan of its filters, found without building the query (``QuerySet._get_direct_get()``)."""

    #: The filters.
    filters: dict[str, Any]
    #: The plan key, the default scope's structure last.
    plan_key: tuple[Any, ...]
    #: The plan found under the key when ``get()`` was called; None for the first query of the key.
    plan: StatementPlan | None
    #: The connection.
    db: DatabaseClient
    #: The default scope when ``get()`` was called - its filters are put in front of the
    #: ``get()``'s own, so their values bind first.
    scope_description: PlanDescription
    #: The filter key of each value of the default scope.
    scope_keys: tuple[str, ...]
