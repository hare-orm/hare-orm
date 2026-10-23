from __future__ import annotations

from typing import TYPE_CHECKING

from hare.sql import functions
from hare.sql.sql_context import SqlContext
from hare.sql.terms import criteria

if TYPE_CHECKING:
    from hare.dialects.base.renderers.term_renderers import TermRenderers


class JsonRenderers:
    """How a dialect writes JSON: objects and arrays built, a value read at a path and compared, a sort
    key of a JSON value, and the tests of a key's presence and a value's type - each dialect gives
    its own SQL of them."""

    @classmethod
    def register(cls, renderers: TermRenderers) -> None:
        """Registers the json renderers on a dialect's renderers.

        Args:
            renderers: The renderers.
        """
        renderers.register(functions.JsonObject, cls.render_json_object)
        renderers.register(functions.JsonArray, cls.render_json_array)
        renderers.register(functions.JsonValue, cls.render_json_value)
        renderers.register(functions.JsonComparand, cls.render_json_comparand)
        renderers.register(functions.JsonSortKey, cls.render_json_sort_key)
        renderers.register(criteria.JSONAttributeCriterion, cls.render_json_attribute)

    @classmethod
    def render_json_object(cls, function: functions.JsonObject, sql_context: SqlContext) -> str:
        """A JSON object built of keys and values."""
        raise NotImplementedError

    @classmethod
    def render_json_array(cls, function: functions.JsonArray, sql_context: SqlContext) -> str:
        """A JSON array built of values."""
        raise NotImplementedError

    @classmethod
    def render_json_value(cls, function: functions.JsonValue, sql_context: SqlContext) -> str:
        """A value written into a JSON document."""
        raise NotImplementedError

    @classmethod
    def render_json_comparand(cls, function: functions.JsonComparand, sql_context: SqlContext) -> str:
        """A JSON value as a comparison of a JSON value takes it."""
        raise NotImplementedError

    @classmethod
    def render_json_sort_key(cls, function: functions.JsonSortKey, sql_context: SqlContext) -> str:
        """The key a JSON value orders by."""
        raise NotImplementedError

    @classmethod
    def render_json_attribute(cls, criterion: criteria.JSONAttributeCriterion, sql_context: SqlContext) -> str:
        """The value at a path of a JSON document."""
        raise NotImplementedError
