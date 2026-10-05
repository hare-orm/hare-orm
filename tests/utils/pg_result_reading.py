"""Reads the rows of a model query two ways - a rust.native.pg PgResult read by its decoded values,
and the same result's PgRow objects - for tests checking that both give the same instances."""

import datetime
from decimal import Decimal
from typing import Any

import pytest

from hare.query.rows.native.hydrate_accelerator import HydrateAccelerator
from hare.time import Timezone
from tests.utils.model_rows_queries import get_model_rows_query


class PgResultReading:
    """The reading of a model query's PgResult, compared with the reading of its PgRow objects."""

    @staticmethod
    async def fetch_result(model: type[Any]) -> tuple[Any, Any, list[str]]:
        """The model's rows as the driver returned them, their reader and the read attribute names.

        Args:
            model: The model.

        Returns:
            The result, the reader and the names - skips the test off rust_pg.
        """
        pg = pytest.importorskip("rust.native.pg")
        compiler = get_model_rows_query(model.objects.all().order_by("pk"))
        compiler._make_query()
        sql, values = compiler.query.get_parameterized_sql()
        _, rows = await compiler._connection.execute(sql, values)
        if not isinstance(rows, pg.PgResult):
            pytest.skip("reads the rows of a rust_pg connection")
        reader = HydrateAccelerator.get_model_reader(
            model,
            compiler._decode_plan,
            compiler._decode_plan_is_partial,
            compiler._connection.dialect.types,
            Timezone.get_aware_zone_name(),
        )
        return rows, reader, [name for name, *_ in compiler._decode_plan]

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
        rows, reader, names = await PgResultReading.fetch_result(model)
        from_result = reader.read(rows)
        from_row_objects = reader.read(list(rows))
        assert len(from_result) == len(from_row_objects)
        for result_instance, row_instance in zip(from_result, from_row_objects, strict=True):
            for name in names:
                PgResultReading.assert_same_value(
                    f"{model.__name__}.{name}", getattr(result_instance, name), getattr(row_instance, name)
                )
