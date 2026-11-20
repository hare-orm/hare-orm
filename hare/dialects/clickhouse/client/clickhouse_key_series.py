from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.dialects.clickhouse.client.constants import (
    CLICKHOUSE_KEY_SERIES_GAP_SQL,
    CLICKHOUSE_KEY_SERIES_NEXT_SQL,
    CLICKHOUSE_KEY_SERIES_SKIP_SQL,
    CLICKHOUSE_KEY_SERIES_TAKE_SQL,
)
from hare.dialects.clickhouse.cluster.clickhouse_placement_plan import ClickhousePlacementPlan

if TYPE_CHECKING:
    from hare.dialects.clickhouse.client.clickhouse_client import ClickhouseClient
    from hare.models.model_binding import ModelBinding


class ClickhouseKeySeries:
    """Allocates generated keys and synchronizes each ClickHouse Keeper series with its model table."""

    @staticmethod
    async def take_generated_keys(client: ClickhouseClient, binding: ModelBinding, count: int) -> list[Any]:
        """The next keys of the model's series - ``generateSerialID`` of the series named after the
        database and the model (``database.app.Model``), which a rename of its table keeps. Before the
        first keys a process takes of a series, the series is moved past the greatest key of the table."""
        await client.synchronize_key_series(binding, checked_once=True)
        series_sql = client.dialect.literals.get_string_literal_sql(client.get_key_series_name(binding))
        rows = await client.execute(
            CLICKHOUSE_KEY_SERIES_TAKE_SQL.format(series=series_sql, count=count),
            returns_rows=True,
            rows_by_position=True,
        )
        return [row[0] for row in rows.rows]

    @staticmethod
    async def get_generated_key_sql(client: ClickhouseClient, binding: ModelBinding) -> str | None:
        """``generateSerialID`` of the model's series - moved past the greatest key of its table first."""
        await client.synchronize_key_series(binding)
        return CLICKHOUSE_KEY_SERIES_NEXT_SQL.format(
            series=client.dialect.literals.get_string_literal_sql(client.get_key_series_name(binding))
        )

    @staticmethod
    def get_key_series_name(client: ClickhouseClient, binding: ModelBinding) -> str:
        """The name of the series of a model's keys - the Keeper keeps one series per name for every
        database of the server, so the database is part of it.

        Args:
            client: The ClickHouse client supplying driver capabilities and connection settings.
            binding: The model.

        Returns:
            ``database.app.Model``.
        """
        database = ClickhousePlacementPlan.get_effective_database(client.database, binding.schema)
        return f"{database}.{binding.full_name}"

    @staticmethod
    async def synchronize_key_series(
        client: ClickhouseClient, binding: ModelBinding, *, checked_once: bool = False
    ) -> None:
        """Moves the model's series past the greatest key of its table - the series hands out the keys
        after it.

        Args:
            client: The ClickHouse client supplying driver capabilities and connection settings.
            binding: The model.
            checked_once: Whether a series this process already moved is left alone.
        """
        root_client: ClickhouseClient = client.get_non_transactional_client()  # type: ignore[assignment]
        synchronized_series = root_client.synchronized_key_series
        series_name = client.get_key_series_name(binding)
        if checked_once and series_name in synchronized_series:
            return
        literals = client.dialect.literals
        rows = await client.execute(
            CLICKHOUSE_KEY_SERIES_GAP_SQL.format(
                series=literals.get_string_literal_sql(series_name),
                key=literals.quote_identifier(binding.fields_db_projection[binding.primary_key_attribute_names[0]]),
                table=literals.qualify_table_name(binding.db_table, binding.schema),
            ),
            returns_rows=True,
            rows_by_position=True,
        )
        gap = int(rows.rows[0][0])
        if gap > 0:
            # A key taken is never handed out again: the series moves by taking the keys it lags by.
            series_sql = literals.get_string_literal_sql(series_name)
            await client.execute(
                CLICKHOUSE_KEY_SERIES_SKIP_SQL.format(series=series_sql, count=gap), returns_rows=True
            )
        synchronized_series.add(series_name)
