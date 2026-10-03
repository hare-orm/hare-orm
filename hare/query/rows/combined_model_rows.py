from __future__ import annotations

import asyncio
import contextlib
import itertools
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, cast

from hare.dialects.base.client.database_client import DatabaseClient
from hare.dialects.base.client.transaction_client import TransactionClient
from hare.dialects.base.constants import CHUNK_SIZE
from hare.exceptions import QueryError
from hare.query.constants import COMBINED_QUERY_APP_COLUMN, COMBINED_QUERY_MODEL_COLUMN
from hare.query.expressions import Value
from hare.query.relation_loading.prefetcher import Prefetcher
from hare.query.rows.hydrate_accelerator import HydrateAccelerator
from hare.query.rows.model_rows import ModelRows
from hare.query.statements.select.select_query import SelectQuery
from hare.sql import Order

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.base.field import Field
    from hare.models import Model
    from hare.query.statements.select.combined_query import CombinedQuery


class CombinedModelRows:
    """How the combined rows of model querysets are read: every branch selects the same columns
    plus two naming the row's model, which picks the model each row is an instance of; an
    annotation decodes through the field of the first branch that knows one, like Django."""

    def __init__(self, model: type[Model], models: set[type[Model]], prefetched_relations: tuple[Any, ...]) -> None:
        """
        Args:
            model: The model of the first queryset.
            models: The models the rows can be instances of.
            prefetched_relations: The relations prefetched on the instances once they are read.
        """
        self.model = model
        self.models = models
        self.prefetched_relations = prefetched_relations
        #: The combined columns in the first branch's order, the model columns among them.
        self.column_names: list[str] = []
        #: The combined columns but the two naming the model.
        self.selects: list[str] = []
        #: The annotation keys the rows carry - the first branch's.
        self.custom_fields: frozenset[str] = frozenset()
        #: Annotation key -> the field its value decodes through.
        self.custom_field_output_fields: dict[str, Field[Any]] = {}
        #: Annotation keys a branch can't decode through a known field.
        self.undecoded_custom_fields: set[str] = set()

    def get_output_names(self) -> list[str]:
        """The names of the combined columns - known once a branch is built."""
        return self.column_names

    def get_output_aliases(self) -> list[str]:
        """The aliases of the combined columns, in output order."""
        return self.column_names

    def get_branch_aliases(self, branch: SelectQuery[Any] | CombinedQuery) -> list[str]:
        """The aliases of a built branch's columns, in output order."""
        return [select.alias or select.name for select in branch.query._selects]

    def get_default_orderings(self) -> list[tuple[str, Order]]:
        """The ordering of unordered rows ``first()``/``last()`` and ``iterator()`` take - the
        primary key, like Django."""
        return [(pk_attr_name, Order.ASC) for pk_attr_name in self.model._meta.pk_attr_names]

    def check_ordering_name(self, field_name: str) -> None:
        """Nothing to reject before a build - the selected columns are known once a branch is
        built (``get_ordering_alias()``)."""

    def get_ordering_alias(self, field_name: str) -> str:
        """The combined column an ordering name reads - a field by its column.

        Raises:
            QueryError: The rows don't select it.
        """
        column_name = self.model._meta.fields_db_projection.get(field_name, field_name)
        if column_name not in self.column_names:
            raise QueryError("Order by field must be in the select list for union queries")
        return column_name

    def take_branch(self, branch: SelectQuery[Any] | CombinedQuery, branch_index: int) -> None:
        """Checks a built branch selects the columns the first one does, and takes each annotation's
        output field from the first branch that knows it.

        Raises:
            QueryError: The branch selects other columns.
        """
        model_columns = (COMBINED_QUERY_APP_COLUMN, COMBINED_QUERY_MODEL_COLUMN)
        column_names = self.get_branch_aliases(branch)
        selects = [name for name in column_names if name not in model_columns]
        is_first_branch = not self.column_names
        if is_first_branch:
            self.column_names = column_names
            self.selects = selects
            self.custom_field_output_fields = {}
            self.undecoded_custom_fields = set()
        elif selects != self.selects:
            raise QueryError("Union queries must have the same select fields")
        if not isinstance(branch, SelectQuery):
            nested_rows = cast("CombinedModelRows", branch._rows)
            if is_first_branch:
                self.custom_fields = nested_rows.custom_fields
            self.undecoded_custom_fields |= nested_rows.undecoded_custom_fields & self.custom_fields
            for key in self.undecoded_custom_fields:
                self.custom_field_output_fields.pop(key, None)
            for key in self.custom_fields - self.undecoded_custom_fields:
                if (output_field := nested_rows.custom_field_output_fields.get(key)) is not None:
                    self.custom_field_output_fields.setdefault(key, output_field)
            return
        if is_first_branch:
            self.custom_fields = frozenset(branch._annotations.keys() - branch._alias_keys - set(model_columns))
        for key in self.custom_fields - self.undecoded_custom_fields:
            output_field = branch._annotation_output_fields.get(key)
            if output_field is None:
                annotation = branch._annotations.get(key)
                if not (isinstance(annotation, Value) and annotation.value is None):
                    self.undecoded_custom_fields.add(key)
                    self.custom_field_output_fields.pop(key, None)
                continue
            self.custom_field_output_fields.setdefault(key, output_field)

    def get_result_reading(self) -> tuple[Any, ...]:
        """What reading the rows of the statement just built needs - kept with its plan."""
        return (
            self.custom_fields,
            tuple(self.custom_field_output_fields.items()),
            frozenset(self.undecoded_custom_fields),
            tuple(self.selects),
            tuple(self.column_names),
        )

    def restore(self, result_reading: tuple[Any, ...]) -> None:
        """Takes what reading the rows needs from the plan the query runs on."""
        custom_fields, custom_field_output_fields, undecoded_custom_fields, selects, column_names = result_reading
        self.custom_fields = custom_fields
        self.custom_field_output_fields = dict(custom_field_output_fields)
        self.undecoded_custom_fields = set(undecoded_custom_fields)
        self.selects = list(selects)
        self.column_names = list(column_names)

    def get_output_columns(self) -> list[tuple[str, str, str, Field[Any] | None]]:
        """The combined columns of the built query, to read from it as a derived table - per
        column its field or annotation name (twice), its column name and the field its value
        decodes through."""
        meta = self.model._meta
        field_names_by_column = {
            column_name: field_name for field_name, column_name in meta.fields_db_projection.items()
        }
        output_columns: list[tuple[str, str, str, Field[Any] | None]] = []
        for column_name in self.selects:
            field_name = field_names_by_column.get(column_name, column_name)
            value_field = meta.fields_map.get(field_name) or self.custom_field_output_fields.get(column_name)
            output_columns.append((field_name, field_name, column_name, value_field))
        return output_columns

    def _get_model_rows(self, db: DatabaseClient) -> ModelRows:
        """How the rows are read into instances on ``db``."""
        return ModelRows(
            self.model,
            db,
            annotations=list(self.custom_fields),
            annotation_fields=self.custom_field_output_fields,
        )

    def read_rows(self, model_rows: ModelRows, db: DatabaseClient, rows: list[Any]) -> list[Model]:
        """The instance of each row - in one ``rust.native.rows`` call where the rows are read by
        position.

        Args:
            model_rows: How the rows are read into instances.
            db: The connection the rows were read on.
            rows: The rows, at least one.

        Returns:
            The instances.
        """
        column_names = list(rows[0].keys())
        if HydrateAccelerator.module is not None and db.features.supports_positional_rows:
            return HydrateAccelerator.run_or_fall_back(
                lambda: model_rows.read_combined_with_accelerator(
                    rows, column_names, self.models, COMBINED_QUERY_APP_COLUMN, COMBINED_QUERY_MODEL_COLUMN
                ),
                lambda: list(
                    model_rows.read_combined(
                        rows, column_names, self.models, COMBINED_QUERY_APP_COLUMN, COMBINED_QUERY_MODEL_COLUMN
                    )
                ),
            )
        return list(
            model_rows.read_combined(
                rows, column_names, self.models, COMBINED_QUERY_APP_COLUMN, COMBINED_QUERY_MODEL_COLUMN
            )
        )

    async def read(self, query: CombinedQuery, sql: str, params: list[Any]) -> list[Any]:
        """Runs the statement, makes each row an instance of the model its model columns name,
        then prefetches the relations asked for - batched over the instances, grouped by their
        model, on the connection the rows were read from."""
        db = query._db
        _, rows = await db.execute(sql, params, returns_rows=True)
        if not rows:
            return []
        model_rows = self._get_model_rows(db)
        instances: list[Model] = []
        if HydrateAccelerator.module is not None and db.features.supports_positional_rows:
            instances = self.read_rows(model_rows, db, rows if type(rows) is list else list(rows))
        else:
            reader = model_rows.read_combined(
                rows, list(rows[0].keys()), self.models, COMBINED_QUERY_APP_COLUMN, COMBINED_QUERY_MODEL_COLUMN
            )
            while chunk := list(itertools.islice(reader, CHUNK_SIZE)):
                instances.extend(chunk)
                await asyncio.sleep(0)
        if instances and self.prefetched_relations:
            instances_by_model: dict[type[Model], list[Model]] = {}
            for instance in instances:
                instances_by_model.setdefault(type(instance), []).append(instance)
            for model, model_instances in instances_by_model.items():
                await Prefetcher(model, db).load(
                    model_instances, *self.prefetched_relations, db_explicitly_chosen=True
                )
        return cast("list[Any]", instances)

    async def stream_batches(
        self, query: CombinedQuery, db: TransactionClient, sql: str, params: list[Any], chunk_size: int
    ) -> AsyncIterator[list[Any]]:
        """Streams the instances off a server-side cursor, a batch of rows at a time.

        Raises:
            QueryError: Relations are prefetched - that needs every instance up front.
        """
        if self.prefetched_relations:
            raise QueryError(
                "stream() does not support prefetch_related() - resolving a prefetch needs every "
                "parent instance up front; use iterator() or plain iteration instead"
            )
        model_rows = self._get_model_rows(db)
        async with contextlib.aclosing(db.stream_batches(sql, params, chunk_size=chunk_size)) as batches:
            async for batch in batches:
                yield self.read_rows(model_rows, db, batch if type(batch) is list else list(batch))
