from __future__ import annotations

import re

from hare.exceptions import UnSupportedError
from hare.sql.sql_context import DEFAULT_SQL_CONTEXT, SqlContext
from hare.sql.terms.term import Term


class Interval(Term):
    # A constant - abstains from the aggregate vote, like ValueWrapper.
    is_aggregate = None

    units = ["years", "months", "days", "hours", "minutes", "seconds", "microseconds"]
    labels = ["YEAR", "MONTH", "DAY", "HOUR", "MINUTE", "SECOND", "MICROSECOND"]

    trim_pattern = re.compile(r"(^0+\.)|(\.0+$)|(^[0\-.: ]+[\-: ])|([\-:. ][0\-.: ]+$)")

    def __init__(
        self,
        years: int = 0,
        months: int = 0,
        days: int = 0,
        hours: int = 0,
        minutes: int = 0,
        seconds: int = 0,
        microseconds: int = 0,
        quarters: int = 0,
        weeks: int = 0,
    ) -> None:
        self.largest = None
        self.smallest = None
        self.is_negative = False

        if quarters:
            self.quarters = quarters
            return

        if weeks:
            self.weeks = weeks
            return

        for unit, label, value in zip(
            self.units,
            self.labels,
            [years, months, days, hours, minutes, seconds, microseconds],
            strict=True,
        ):
            if value:
                int_value = int(value)
                setattr(self, unit, abs(int_value))
                if self.largest is None:
                    self.largest = label
                    self.is_negative = int_value < 0
                self.smallest = label

    def __str__(self) -> str:
        return self.get_sql(DEFAULT_SQL_CONTEXT)

    def get_expression_and_unit(self) -> tuple[str, str]:
        """The interval's quantity text and its unit (``DAY``, ``HOUR_MINUTE``, ...)."""
        if self.largest == "MICROSECOND":
            expression = getattr(self, "microseconds")
            unit = "MICROSECOND"

        elif hasattr(self, "quarters"):
            expression = getattr(self, "quarters")
            unit = "QUARTER"

        elif hasattr(self, "weeks"):
            expression = getattr(self, "weeks")
            unit = "WEEK"

        else:
            # Create the whole expression but trim out the unnecessary fields
            expression = "{years}-{months}-{days} {hours}:{minutes}:{seconds}.{microseconds}".format(
                years=getattr(self, "years", 0),
                months=getattr(self, "months", 0),
                days=getattr(self, "days", 0),
                hours=getattr(self, "hours", 0),
                minutes=getattr(self, "minutes", 0),
                seconds=getattr(self, "seconds", 0),
                microseconds=getattr(self, "microseconds", 0),
            )
            expression = self.trim_pattern.sub("", expression)
            if self.is_negative:
                expression = "-" + expression

            if self.largest != self.smallest:
                unit = f"{self.largest}_{self.smallest}"
            elif self.largest is None:
                # Set default unit with DAY
                unit = "DAY"
            else:
                unit = self.largest

        return str(expression), unit

    def get_sql(self, sql_context: SqlContext) -> str:
        renderer = sql_context.dialect.renderers.get(type(self))
        if renderer is None:
            raise UnSupportedError(f"Interval has no SQL for the {sql_context.dialect} dialect")
        return renderer(self, sql_context)
