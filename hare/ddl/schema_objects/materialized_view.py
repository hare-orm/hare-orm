from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

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
        with_data: Fill the view when it is created; False leaves it unreadable until refreshed.
        unique_columns: Columns of the view a unique index is built over - a concurrent refresh
            needs one.

    Raises:
        ConfigurationError: See ``View``; ``with_data`` isn't a bool, or ``unique_columns`` isn't a
            sequence of distinct non-empty column names.
    """

    with_data: bool = True
    unique_columns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(self.with_data, bool):
            raise ConfigurationError(
                f"MaterializedView {self.name!r}: with_data must be a bool, got {self.with_data!r}"
            )
        if isinstance(cast("object", self.unique_columns), str) or not all(
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
        options: dict[str, Any] = {}
        if not self.with_data:
            options["with_data"] = False
        if self.unique_columns:
            options["unique_columns"] = self.unique_columns
        return options
