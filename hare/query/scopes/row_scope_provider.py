from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from hare.models import Model
    from hare.query.expressions import Expression, Q
    from hare.query.scopes.row_visibility import RowVisibility


class RowScopeProvider(Protocol):
    """Declares an ORM visibility predicate independently of queryset construction.

    Called during preparation, with the tenant and visibility of that execution. Return
    Q, a boolean Expression, or None. Expressions follow Q.from_expression: their SQL
    must be a criterion or their output field must be BooleanField. A bare non-boolean
    F reference is rejected instead of being treated as truthy. The default manager's
    provider also governs relation traversal;
    another manager's provider governs queries started through that manager. Database
    row-level security, raw SQL and referential-integrity checks are separate policies.
    """

    def get_condition(self, model: type[Model], visibility: RowVisibility) -> Q | Expression | None:
        """Returns the condition for this model and visibility without issuing database IO."""
        ...
