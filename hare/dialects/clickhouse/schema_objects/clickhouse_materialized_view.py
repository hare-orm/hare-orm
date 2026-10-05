from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Self, cast

from hare.ddl.raw_sql_term import RawSQLTerm
from hare.ddl.schema_objects.materialized_view import MaterializedView
from hare.dialects.clickhouse.schema.constants import CLICKHOUSE_REFRESH_SCHEDULE_PATTERN
from hare.exceptions import ConfigurationError


@dataclass(frozen=True)
class ClickhouseMaterializedView(MaterializedView):
    """A materialized view with what ClickHouse alone gives one - the table it writes its rows into,
    the engine of the storage of its own, a refresh on a schedule::

        class Visit(Model):
            class Meta:
                materialized_views = [
                    ClickhouseMaterializedView(
                        "visits_by_site",
                        RawSQLTerm("SELECT site, count() AS visits FROM visit GROUP BY site"),
                        engine="SummingMergeTree",
                        order_by=("site",),
                    )
                ]

    A ClickHouse materialized view follows the rows inserted into the table its query reads: each
    inserted block goes through the query and into the view. A view with ``refresh`` holds the rows of
    its whole query as of its last refresh instead. A plain ``MaterializedView`` is kept by a
    ``MergeTree`` sorted by its ``unique_columns``.

    Args:
        name: The view's name.
        query: The ``SELECT`` - as ``View.query``.
        with_data: Fill the view with the rows its query gives when it is created.
        unique_columns: What the rows are sorted by when ``order_by`` names nothing.
        to: The table the view writes its rows into, in place of a storage of its own.
        engine: The engine of the view's own storage, with its arguments.
        order_by: What its own storage is sorted by - columns of the view, and ``RawSQLTerm``
            expressions.
        partition_by: ``RawSQLTerm`` of the expression its own storage is partitioned by.
        refresh: The schedule the view is refreshed on - ``"EVERY 1 HOUR"``, ``"AFTER 30 MINUTE"``,
            with ``OFFSET ...`` and ``RANDOMIZE FOR ...`` as ClickHouse takes them.
        append: A refresh adds its rows to the ones the view holds, instead of replacing them.
        depends_on: The views refreshed on a schedule this one is refreshed after.

    Raises:
        ConfigurationError: See ``MaterializedView``; an argument of another type, ``to`` together
            with a storage of the view's own, ``append`` or ``depends_on`` without ``refresh``.
    """

    to: str | None = None
    engine: str = "MergeTree"
    order_by: tuple[str | RawSQLTerm, ...] = ()
    partition_by: RawSQLTerm | None = None
    refresh: str | None = None
    append: bool = False
    depends_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        super().__post_init__()
        owner = f"ClickhouseMaterializedView {self.name!r}"
        if self.to is not None and (not isinstance(self.to, str) or not self.to.strip()):
            raise ConfigurationError(f"{owner}: to must be a table name, got {self.to!r}")
        if not isinstance(self.engine, str) or not self.engine.strip():
            raise ConfigurationError(f"{owner}: engine must be a non-empty string, got {self.engine!r}")
        if isinstance(cast("object", self.order_by), str) or not all(
            (isinstance(key, str) and key) or (isinstance(key, RawSQLTerm) and key.sql.strip())
            for key in self.order_by
        ):
            raise ConfigurationError(
                f"{owner}: order_by must be a sequence of column names and RawSQLTerm(...) expressions, "
                f"got {self.order_by!r}"
            )
        if self.partition_by is not None and (
            not isinstance(self.partition_by, RawSQLTerm) or not self.partition_by.sql.strip()
        ):
            raise ConfigurationError(
                f"{owner}: partition_by takes RawSQLTerm(...) of a non-empty SQL expression, got {self.partition_by!r}"
            )
        if self.refresh is not None and (
            not isinstance(self.refresh, str)
            or not CLICKHOUSE_REFRESH_SCHEDULE_PATTERN.fullmatch(self.refresh.strip())
        ):
            raise ConfigurationError(
                f'{owner}: refresh takes a schedule - "EVERY 1 HOUR", "AFTER 30 MINUTE", with OFFSET and '
                f"RANDOMIZE FOR - got {self.refresh!r}"
            )
        if not isinstance(self.append, bool):
            raise ConfigurationError(f"{owner}: append must be a bool, got {self.append!r}")
        if isinstance(cast("object", self.depends_on), str) or not all(
            isinstance(view_name, str) and view_name for view_name in self.depends_on
        ):
            raise ConfigurationError(f"{owner}: depends_on must be a sequence of view names, got {self.depends_on!r}")
        object.__setattr__(self, "order_by", tuple(self.order_by))
        object.__setattr__(self, "depends_on", tuple(self.depends_on))
        if self.to is not None and (self.engine != "MergeTree" or self.order_by or self.partition_by is not None):
            raise ConfigurationError(
                f"{owner}: a view writing to {self.to!r} has no storage of its own - engine, order_by and "
                "partition_by are those of that table"
            )
        if self.refresh is None and (self.append or self.depends_on):
            raise ConfigurationError(f"{owner}: append and depends_on are of a view with a refresh schedule")

    @classmethod
    def from_materialized_view(cls, view: MaterializedView) -> Self:
        """A materialized view as ClickHouse keeps it.

        Args:
            view: The view.

        Returns:
            The view itself when it is one of ClickHouse's, else one of a storage of its own.
        """
        if isinstance(view, cls):
            return view
        return cls(name=view.name, query=view.query, with_data=view.with_data, unique_columns=view.unique_columns)

    def get_sort_keys(self) -> tuple[str | RawSQLTerm, ...]:
        """What the view's own storage is sorted by.

        Returns:
            ``order_by``, or ``unique_columns`` where it names nothing.
        """
        return self.order_by or self.unique_columns

    def get_options(self) -> dict[str, Any]:
        options = super().get_options()
        if self.to is not None:
            options["to"] = self.to
        if self.engine != "MergeTree":
            options["engine"] = self.engine
        if self.order_by:
            options["order_by"] = self.order_by
        if self.partition_by is not None:
            options["partition_by"] = self.partition_by
        if self.refresh is not None:
            options["refresh"] = self.refresh
        if self.append:
            options["append"] = True
        if self.depends_on:
            options["depends_on"] = self.depends_on
        return options
