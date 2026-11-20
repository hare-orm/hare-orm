from __future__ import annotations

from typing import Any

from clickhouse_connect.datatypes.dynamic import SharedVariant

from hare.dialects.clickhouse.drivers.clickhouse_shared_variant_values import ClickhouseSharedVariantValues


class ClickhouseConnectSharedVariantType(SharedVariant, registered=False):
    """Decode shared values locally, including exact decimals and nested containers."""

    def _read_column_binary(self, source: Any, num_rows: int, context: Any, read_state: Any) -> list[Any]:
        return [
            None if value is None else ClickhouseSharedVariantValues.decode(value)
            for value in source.read_str_col(num_rows, None)
        ]
