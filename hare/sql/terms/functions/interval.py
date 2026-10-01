from __future__ import annotations

import re
from typing import TYPE_CHECKING

from hare.exceptions import UnSupportedError
from hare.sql.context import DEFAULT_SQL_CONTEXT, SqlContext
from hare.sql.terms.base.term import Term

if TYPE_CHECKING:
    pass


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
            expr = getattr(self, "microseconds")
            unit = "MICROSECOND"

        elif hasattr(self, "quarters"):
            expr = getattr(self, "quarters")
            unit = "QUARTER"

        elif hasattr(self, "weeks"):
            expr = getattr(self, "weeks")
            unit = "WEEK"

        else:
            # Create the whole expression but trim out the unnecessary fields
            expr = "{years}-{months}-{days} {hours}:{minutes}:{seconds}.{microseconds}".format(
                years=getattr(self, "years", 0),
                months=getattr(self, "months", 0),
                days=getattr(self, "days", 0),
                hours=getattr(self, "hours", 0),
                minutes=getattr(self, "minutes", 0),
                seconds=getattr(self, "seconds", 0),
                microseconds=getattr(self, "microseconds", 0),
            )
            expr = self.trim_pattern.sub("", expr)
            if self.is_negative:
                expr = "-" + expr

            if self.largest != self.smallest:
                unit = f"{self.largest}_{self.smallest}"
            elif self.largest is None:
                # Set default unit with DAY
                unit = "DAY"
            else:
                unit = self.largest

        return str(expr), unit

    def get_sql(self, ctx: SqlContext) -> str:
        renderer = ctx.dialect.renderers.get(type(self))
        if renderer is None:
            raise UnSupportedError(f"Interval has no SQL for the {ctx.dialect} dialect")
        return renderer(self, ctx)
