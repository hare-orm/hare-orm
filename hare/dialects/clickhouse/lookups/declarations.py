from __future__ import annotations

from hare.dialects.clickhouse.lookups.clickhouse_json_value_text import ClickhouseJsonValueText


class ClickhouseJsonNullText(ClickhouseJsonValueText):
    """The JSON null compared with a path of a JSON column, which keeps no null - equal to a missing
    path, as the path reads NULL there; ordered below every value."""
