from __future__ import annotations

from hare.dialects.clickhouse.lookups.clickhouse_json_value_text import ClickhouseJsonValueText
from hare.sql.terms.values.value_wrapper import ValueWrapper


class ClickhouseJsonNullText(ClickhouseJsonValueText):
    """The JSON null compared with a path of a JSON column, which keeps no null - equal to a missing
    path, as the path reads NULL there; ordered below every value."""


class ClickhouseStoredJsonText(ValueWrapper):
    """The JSON text a field keeping its text (``JSONField.keeps_json_text``) is compared with - a
    ``String`` column of a connection storing other JSON fields as ``JSON`` ones."""
