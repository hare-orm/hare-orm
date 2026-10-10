from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from hare.dialects.postgresql.renderers.constants import (
    POSTGRESQL_NAIVE_TIME_OFFSET,
    POSTGRESQL_NOW_DATE_SQL_TEMPLATE,
    POSTGRESQL_NOW_OFFSET_ZONE_TEMPLATE,
    POSTGRESQL_NOW_SQL,
    POSTGRESQL_NOW_TIME_SQL_TEMPLATE,
    POSTGRESQL_RANDOM_HEX_SQL,
    POSTGRESQL_UUID_V7_SQL,
)
from hare.exceptions import ConfigurationError
from hare.fields.db_defaults.now import Now
from hare.fields.db_defaults.random_hex import RandomHex
from hare.fields.db_defaults.uuid_v7 import UuidV7
from hare.fields.enums import NowValueType
from hare.time import Timezone

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.renderers.term_renderers import TermRenderers
    from hare.sql.sql_context import SqlContext


class PostgresqlDefaults:
    """How PostgreSQL writes the database-level defaults of ``db_default``."""

    @classmethod
    def register(cls, renderers: TermRenderers) -> None:
        """Adds the renderers of ``SqlDefault`` subclasses to PostgreSQL's term renderers.

        Args:
            renderers: PostgreSQL's term renderers.
        """
        renderers.register(Now, cls.render_now)
        renderers.register(RandomHex, cls.render_random_hex)
        renderers.register(UuidV7, cls.render_uuid_v7)

    @staticmethod
    def get_zone_sql(default: Now) -> str:
        """The zone a date/wall clock default is taken in, as an ``AT TIME ZONE`` operand.

        Args:
            default: The default.

        Returns:
            The configured zone's name under ``use_timezone=True``; the system's local zone otherwise
            (its name when ``tzlocal`` is installed, its current UTC offset without it).
        """
        if Timezone.get_use_timezone():
            return default.get_quoted_text(Timezone.name())
        try:
            return default.get_quoted_text(Timezone.get_local_zone_name())
        except ConfigurationError:
            local_offset = datetime.datetime.now().astimezone().utcoffset() or datetime.timedelta(0)
            return POSTGRESQL_NOW_OFFSET_ZONE_TEMPLATE.format(offset=default.get_offset_text(local_offset))

    @classmethod
    def render_now(cls, default: Now, sql_context: SqlContext) -> str:
        """The moment of the statement - a date column's the current date, a time column's the
        current wall clock with the offset a Python-side value of the column is bound with.

        Args:
            default: The default.
            sql_context: The rendering context.

        Returns:
            The SQL.
        """
        if default.value_type == NowValueType.DATETIME:
            return POSTGRESQL_NOW_SQL
        zone_sql = cls.get_zone_sql(default)
        if default.value_type == NowValueType.DATE:
            return POSTGRESQL_NOW_DATE_SQL_TEMPLATE.format(zone=zone_sql)
        offset_text = (
            default.get_configured_offset_text() if Timezone.get_use_timezone() else POSTGRESQL_NAIVE_TIME_OFFSET
        )
        return POSTGRESQL_NOW_TIME_SQL_TEMPLATE.format(zone=zone_sql, offset=offset_text)

    @staticmethod
    def render_random_hex(default: RandomHex, sql_context: SqlContext) -> str:
        return POSTGRESQL_RANDOM_HEX_SQL

    @staticmethod
    def render_uuid_v7(default: UuidV7, sql_context: SqlContext) -> str:
        return POSTGRESQL_UUID_V7_SQL
