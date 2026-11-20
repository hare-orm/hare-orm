"""Compare positional and named model hydration of native PostgreSQL rows."""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest

from hare.core.hare_context import HareContext
from hare.models.instances.dirty_fields import DirtyFields
from hare.native.native_modules import NativeModules
from hare.query.rows.model_rows.model_columns import ModelColumns
from tests.utils.database_under_test import DatabaseUnderTest
from tests.utils.model_rows_queries import get_model_rows_query

if TYPE_CHECKING:
    from hare.query.rows.native.hydration_layout import HydrationEntry


class PgResultReading:
    """The reading of a model query's PgResult, compared with the reading of its PgRow objects."""

    @staticmethod
    async def fetch_result(model: type[Any]) -> tuple[Any, tuple[HydrationEntry, ...], bool]:
        """The driver's rows, their decode plan and whether the model projection is partial.

        Args:
            model: The model.

        Returns:
            The result, its decode plan and the projection flag. Skips the test off rust_pg.
        """
        if DatabaseUnderTest.get_driver().name != "postgresql":
            pytest.skip("reads the rows of a rust_pg connection")
        from rust.native import pg

        compiler = get_model_rows_query(model.objects.all().order_by("pk"))
        compiler._make_query()
        sql, values = compiler.query.get_parameterized_sql()
        _, rows = await compiler._connection.execute(sql, values)
        if not isinstance(rows, pg.PgResult):
            pytest.skip("reads the rows of a rust_pg connection")
        decode_plan = compiler._decode_plan
        assert decode_plan is not None
        return rows, decode_plan, compiler._decode_plan_is_partial

    @staticmethod
    def assert_same_value(where: str, result_value: Any, row_value: Any) -> None:
        """Equal values of the same type - Decimal digits, datetime tzinfo and fold included, and
        the items of a list or the parts of a range alike.

        Args:
            where: What the value is, for the failure message.
            result_value: The value read from the PgResult.
            row_value: The value read from the PgRow.
        """
        assert result_value == row_value, f"{where}: {result_value!r} != {row_value!r}"
        assert type(result_value) is type(row_value), where
        if isinstance(row_value, Decimal):
            assert result_value.as_tuple() == row_value.as_tuple(), where
        if isinstance(row_value, datetime.datetime):
            assert result_value.tzinfo is row_value.tzinfo, where
            assert result_value.fold == row_value.fold, where
        if isinstance(row_value, list):
            for result_item, row_item in zip(result_value, row_value, strict=True):
                PgResultReading.assert_same_value(where, result_item, row_item)
        for part in ("lower", "upper"):
            if hasattr(row_value, "is_empty") and hasattr(row_value, part):
                PgResultReading.assert_same_value(where, getattr(result_value, part), getattr(row_value, part))

    @staticmethod
    async def assert_result_read_as_rows(model: type[Any], create_kwargs_list: list[dict[str, Any]]) -> None:
        """Creates the rows, then checks the PgResult and its PgRow objects read the same.

        Args:
            model: The model.
            create_kwargs_list: The keyword arguments of each row.
        """
        await model.objects.bulk_create([model(**kwargs) for kwargs in create_kwargs_list])
        rows, entries, is_partial = await PgResultReading.fetch_result(model)
        context = HareContext.require_current()
        binding = context.apps.get_binding(model)
        reader = ModelColumns.from_entries(binding, entries, tuple(range(len(entries))), is_partial)
        names = [name for name, *_ in entries]
        named_reader = ModelColumns.from_entries(
            binding, entries, tuple(binding.fields_db_projection[name] for name in names), is_partial
        )
        positional_rows, _ = rows.positional()
        from_result = NativeModules.rows.ModelResultLayout([reader], ()).read_batch(
            positional_rows, 0, len(positional_rows), None, context, DirtyFields.snapshot_dirty_fields
        )
        from_row_objects = NativeModules.rows.ModelResultLayout([named_reader], ()).read_batch(
            rows, 0, len(rows), None, context, DirtyFields.snapshot_dirty_fields
        )
        for result_instance, row_instance in zip(from_result, from_row_objects, strict=True):
            for name in names:
                PgResultReading.assert_same_value(
                    f"{model.__name__}.{name}", getattr(result_instance, name), getattr(row_instance, name)
                )
