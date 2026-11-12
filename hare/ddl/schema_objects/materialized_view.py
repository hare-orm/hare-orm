from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hare.ddl.schema_objects.view import View
from hare.exceptions import ConfigurationError


@dataclass(frozen=True)
class MaterializedView(View):
    """A materialized view a model declares in ``Meta.materialized_views`` - the rows of a
    ``SELECT`` kept in the database until refreshed (``RefreshMaterializedView``,
    ``QuerySet.refresh_materialized_view()``).

    Args:
        name: The view's name.
        query: The ``SELECT`` - as ``View.query``.
        with_data: Fill the view when it is created. Without this, the view may be empty
            or unreadable until refreshed, depending on the dialect. The schema objects
            guide describes each supported database's behavior.
        unique_columns: Columns of the view's unique index where the dialect supports
            it. A concurrent refresh may require this index.

    Raises:
        ConfigurationError: See ``View``; ``unique_columns`` isn't a sequence of distinct non-empty
            column names.
    """

    with_data: bool = True
    unique_columns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        super().__post_init__()
        if isinstance(self.unique_columns, str) or not all(  # type: ignore[unreachable]
            isinstance(column, str) and column for column in self.unique_columns
        ):
            raise ConfigurationError(
                f"MaterializedView {self.name!r}: unique_columns must be a sequence of column names, "
                f"got {self.unique_columns!r}"
            )
        if len(set(self.unique_columns)) != len(self.unique_columns):
            raise ConfigurationError(
                f"MaterializedView {self.name!r}: unique_columns names a column twice: {self.unique_columns!r}"
            )
        object.__setattr__(self, "unique_columns", tuple(self.unique_columns))

    def get_options(self) -> dict[str, Any]:
        options = super().get_options()
        if not self.with_data:
            options["with_data"] = False
        if self.unique_columns:
            options["unique_columns"] = self.unique_columns
        return options
