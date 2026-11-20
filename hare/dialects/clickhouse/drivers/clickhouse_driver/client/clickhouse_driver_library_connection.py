from __future__ import annotations

from typing import Any

from clickhouse_driver.connection import Connection

from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_block_input import (
    ClickhouseDriverBlockInput,
)
from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_block_output import (
    ClickhouseDriverBlockOutput,
)


class ClickhouseDriverLibraryConnection(Connection):  # type: ignore[misc]
    """A connection with local column codecs and the library's ordinary transport lifecycle."""

    def get_block_in_stream(self) -> Any:
        return ClickhouseDriverBlockInput(super().get_block_in_stream())

    def get_block_out_stream(self) -> Any:
        return ClickhouseDriverBlockOutput(super().get_block_out_stream())
