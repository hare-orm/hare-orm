from __future__ import annotations

import asyncio
import contextlib
import itertools
from collections import defaultdict
from collections.abc import AsyncIterator
from copy import copy
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from hare.dialects.base.client.database_client import DatabaseClient
from hare.dialects.base.constants import CHUNK_SIZE
from hare.dialects.base.results.statement_result import StatementResult
from hare.exceptions import QueryError
from hare.query.constants import COMBINED_QUERY_APP_COLUMN, COMBINED_QUERY_MODEL_COLUMN
from hare.query.execution.query_execution import QueryExecution
from hare.query.expressions import Value
from hare.query.hare_column_names import HareColumnNames
from hare.query.relation_loading.prefetching.prefetcher import Prefetcher
from hare.query.rows.model_rows.model_rows import ModelRows
from hare.query.statements.constants import VALUES_SET_OPERATION_COLUMN_PREFIX
from hare.query.statements.projections.derived_projection import DerivedProjection
from hare.query.statements.select.select_query import SelectQuery
from hare.sql import Order

if TYPE_CHECKING:
    from hare.core.hare_context import HareContext
    from hare.fields.field import Field
    from hare.models import Model
    from hare.models.model_binding import ModelBinding
    from hare.query.statements.projections.declarations import SelectedOutputColumn
    from hare.query.statements.select.combined_query import CombinedQuery
    from hare.sql.builder.queries.query_builder import QueryBuilder


class CombinedModelRows:
    """How the combined rows of model querysets are read: every branch selects the same columns
    plus two naming the row's model, which picks the model each row is an instance of; an
    annotation decodes through the field of the first branch that knows one, like Django."""

    def __init__(self, binding: ModelBinding, models: set[type[Model]], prefetched_relations: tuple[Any, ...]) -> None:
        """
        Args:
            binding: The prepared binding of the first queryset's model.
            models: The models the rows can be instances of.
            prefetched_relations: The relations prefetched on the instances once they are read.
        """
        self._binding = binding
        self.models = models
        self.prefetched_relations = prefetched_relations
        bindings = tuple(binding.graph.get_binding(model) for model in models)
        #: The names of the two columns naming each row's model - its app and its class.
        self.app_column = HareColumnNames.get_free_name(COMBINED_QUERY_APP_COLUMN, bindings)
        self.model_column = HareColumnNames.get_free_name(COMBINED_QUERY_MODEL_COLUMN, bindings)
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
        return [f"{VALUES_SET_OPERATION_COLUMN_PREFIX}{i}" for i in range(len(self.column_names))]

    def get_default_orderings(self) -> list[tuple[str, Order]]:
        """The ordering of unordered rows ``first()``/``last()`` and ``iterator()`` take - the
        primary key, like Django."""
        return [
            (primary_key_attribute_name, Order.ASC)
            for primary_key_attribute_name in self._binding.primary_key_attribute_names
        ]

    def check_ordering_name(self, field_name: str) -> None:
        """Nothing to reject before a build - the selected columns are known once a branch is
        built (``get_ordering_alias()``)."""

    def get_ordering_alias(self, field_name: str) -> str:
        """The combined column an ordering name reads - a field by its column.

        Raises:
            QueryError: The rows don't select it.
        """
        if field_name not in self.column_names:
            raise QueryError("Order by field must be in the select list for union queries")
        return self.get_output_aliases()[self.column_names.index(field_name)]

    def get_branch_projection(
        self, branch: SelectQuery[Any] | CombinedQuery, public_columns: list[tuple[str, int]]
    ) -> tuple[QueryBuilder, list[str]]:
        """Map logical model outputs to branch cells after its DISTINCT, ordering and slice.

        Physical source columns may share names with another field or annotation. Internal
        projection aliases keep those cells distinct; SQL-only cells remain in the inner
        row identity and are trimmed before the set operation pairs its visible columns.
        """
        public_slots = dict(public_columns)
        # Preserve ALL source cells through DISTINCT/order/slice, then trim SQL-only cells.
        source = copy(branch.query)
        with_clauses, source._with = source._with, []
        projection = DerivedProjection(source, column_prefix=VALUES_SET_OPERATION_COLUMN_PREFIX)
        inner = projection.query
        aliases = [projection.columns[public_slots[name]].name for name in self.column_names]
        if len(aliases) == len(projection.columns):
            # Keep unknown literals in the set operation's type-inference scope.
            inner._with = with_clauses
            return inner, aliases
        query = branch._connection.query_class.from_(inner).select(
            *(inner.field(alias).as_(alias) for alias in aliases)
        )
        query._with = with_clauses
        return query, aliases

    def take_branch(self, branch: SelectQuery[Any] | CombinedQuery) -> list[tuple[str, int]]:
        """Accept a branch's columns and take each annotation's output field from the first branch
        that knows it. Return the public columns with their positions in the branch.

        Raises:
            QueryError: The branch selects other columns.
        """
        model_columns = (self.app_column, self.model_column)
        if isinstance(branch, SelectQuery):
            public_columns = [
                (name, position)
                for position, column in enumerate(branch._get_output_columns())
                if (name := column[0]) is not None
            ]
        else:
            public_columns = [(name, position) for position, name in enumerate(branch._rows.get_output_names())]
        column_names = [name for name, _ in public_columns]
        selects = [name for name in column_names if name not in model_columns]
        is_first_branch = not self.column_names
        if is_first_branch:
            self.column_names = column_names
            self.selects = selects
        elif set(selects) != set(self.selects):
            raise QueryError("Union queries must have the same select fields")
        if not isinstance(branch, SelectQuery):
            nested_rows: CombinedModelRows = branch._rows  # type: ignore[assignment]
            if is_first_branch:
                self.custom_fields = nested_rows.custom_fields
            self.undecoded_custom_fields |= nested_rows.undecoded_custom_fields & self.custom_fields
            for key in self.undecoded_custom_fields:
                self.custom_field_output_fields.pop(key, None)
            for key in self.custom_fields - self.undecoded_custom_fields:
                if (output_field := nested_rows.custom_field_output_fields.get(key)) is not None:
                    self.custom_field_output_fields.setdefault(key, output_field)
            return public_columns
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
        return public_columns

    def get_result_reading(self) -> tuple[Any, ...]:
        """The annotation names and immutable field mapping used to read a completed statement.

        Mutable branch accumulators are not retained by the plan or reconstructed on a hit.
        A later build gets a new CombinedModelRows from the query's build preparation.
        """
        return self.custom_fields, MappingProxyType(dict(self.custom_field_output_fields)), tuple(self.column_names)

    def get_output_columns(self) -> list[SelectedOutputColumn]:
        """Every combined cell in SQL order; model discriminators have no public identity."""
        meta = self._binding
        return [
            (name, name, alias, meta.fields_map.get(name) or self.custom_field_output_fields.get(name))
            if name in self.selects
            else (None, None, alias, None)
            for name, alias in zip(self.column_names, self.get_output_aliases(), strict=True)
        ]

    def _get_model_rows(self, connection: DatabaseClient, result_reading: tuple[Any, ...]) -> ModelRows:
        """How the rows are read into instances with the dialect of ``connection``."""
        custom_fields, custom_field_output_fields, output_names = result_reading
        return ModelRows(
            self._binding,
            connection.dialect.types,
            annotations=tuple(custom_fields),
            combined_output_names=output_names,
            annotation_fields=custom_field_output_fields,
        )

    async def read(
        self, query: CombinedQuery, sql: str, parameters: list[Any], result_reading: tuple[Any, ...]
    ) -> list[Any]:
        """Runs the statement, makes each row an instance of the model its model columns name,
        then prefetches the relations asked for - batched over the instances, grouped by their
        model, on the connection the rows were read from."""
        connection = query._connection
        result = await QueryExecution.fetch_rows(connection, sql, parameters, retry_read=query.is_read_only)
        if not result.rows:
            return []
        model_rows = self._get_model_rows(connection, result_reading)
        instances: list[Model] = []
        reader = model_rows.read_combined(
            connection,
            result.rows,
            result.column_names,
            self.models,
            self.app_column,
            self.model_column,
            context=query._context,
        )
        while chunk := list(itertools.islice(reader, CHUNK_SIZE)):
            instances.extend(chunk)
            await asyncio.sleep(0)
        if instances and self.prefetched_relations:
            instances_by_model: defaultdict[type[Model], list[Model]] = defaultdict(list)
            for instance in instances:
                instances_by_model[type(instance)].append(instance)
            for model_instances in instances_by_model.values():
                await Prefetcher(model_instances[0]._binding, connection, query._context).load(
                    model_instances, *self.prefetched_relations, db_explicitly_chosen=True
                )
        return instances

    async def stream_batches(
        self,
        connection: DatabaseClient,
        sql: str,
        parameters: list[Any],
        chunk_size: int,
        result_reading: tuple[Any, ...],
        *,
        context: HareContext | None = None,
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
        model_rows = self._get_model_rows(connection, result_reading)
        reader = None
        async with contextlib.aclosing(
            connection._stream_built_batches(sql, parameters, chunk_size=chunk_size)
        ) as batches:
            async for batch in batches:
                if not batch:
                    yield []
                    continue
                if reader is None:
                    column_names = StatementResult.get_row_column_names(batch)
                    reader = model_rows.get_combined_reader(
                        connection,
                        column_names,
                        self.models,
                        self.app_column,
                        self.model_column,
                        context=context,
                    )
                yield list(reader(batch))
