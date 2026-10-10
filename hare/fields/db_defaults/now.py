from __future__ import annotations

import datetime

from hare.fields.db_defaults.sql_default import SqlDefault
from hare.fields.enums import NowValueType
from hare.time import Timezone
from hare.time.system_clock import SystemClock


class Now(SqlDefault):
    """The current moment as a ``db_default`` - the moment of the statement (``STATEMENT_TIMESTAMP()``
    on PostgreSQL), with microsecond precision. On SQLite it is rendered as exactly the text a
    ``DatetimeField`` value is written as, so filters and ``get_or_create()`` match a row this
    default wrote. A ``DateField``/``TimeField`` gets the current date or wall-clock time in the
    configured zone. A dialect without a renderer gets
    ``CURRENT_TIMESTAMP``/``CURRENT_DATE``/``CURRENT_TIME``.

    Example::

        class MyModel(Model):
            created_at = fields.DatetimeField(db_default=Now())

    Args:
        value_type: The type the column holds - set by the field.
    """

    def __init__(self, value_type: NowValueType = NowValueType.DATETIME) -> None:
        super().__init__("CURRENT_TIMESTAMP")
        self.value_type = NowValueType(value_type)

    def __repr__(self) -> str:
        return "Now()"

    def get_for_value_type(self, value_type: NowValueType) -> Now:
        """This default for a column holding ``value_type`` values.

        Args:
            value_type: The type of value the column holds.

        Returns:
            ``self`` when it already renders that type, a new ``Now`` otherwise.
        """
        return self if self.value_type == value_type else Now(value_type)

    def get_standard_sql(self) -> str:
        if self.value_type == NowValueType.DATE:
            return "CURRENT_DATE"
        if self.value_type == NowValueType.TIME:
            return "CURRENT_TIME"
        return self.sql

    @staticmethod
    def get_offset_text(offset: datetime.timedelta) -> str:
        """A UTC offset as ``+HH:MM`` (``+HH:MM:SS`` when it has seconds).

        Args:
            offset: The offset.

        Returns:
            The offset text.
        """
        sign = "-" if offset < datetime.timedelta(0) else "+"
        total_seconds = int(abs(offset).total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        text = f"{sign}{hours:02d}:{minutes:02d}"
        return f"{text}:{seconds:02d}" if seconds else text

    @staticmethod
    def get_quoted_text(text: str) -> str:
        """A SQL string literal.

        Args:
            text: The text.

        Returns:
            The text in single quotes, embedded quotes doubled.
        """
        return "'" + text.replace("'", "''") + "'"

    @staticmethod
    def get_constant_utc_offset(zone: datetime.tzinfo) -> datetime.timedelta | None:
        """The UTC offset of a zone that doesn't observe DST this year.

        Args:
            zone: The zone.

        Returns:
            The offset, or None when it differs between January and July.
        """
        year = SystemClock.get_utc_now().year
        winter_offset = datetime.datetime(year, 1, 1, tzinfo=datetime.UTC).astimezone(zone).utcoffset()
        summer_offset = datetime.datetime(year, 7, 1, tzinfo=datetime.UTC).astimezone(zone).utcoffset()
        return winter_offset if winter_offset == summer_offset else None

    @classmethod
    def get_configured_offset_text(cls) -> str:
        """The offset a ``TimeField`` value written from Python carries under ``use_timezone=True``.

        Returns:
            The configured zone's standard offset.
        """
        fixed_offset = Timezone.get_fixed_offset().utcoffset(None)
        return cls.get_offset_text(fixed_offset if fixed_offset is not None else datetime.timedelta(0))
