from hare.query.functions.datetime.extract import Extract
from hare.query.functions.datetime.trunc import Trunc
from hare.utils.declared_subclass import DeclaredSubclass

ExtractYear = DeclaredSubclass.make(
    Extract,
    "ExtractYear",
    __package__,
    """The year: ``ExtractYear("created_at")``.""",
    lookup_name="year",
)


ExtractIsoYear = DeclaredSubclass.make(
    Extract,
    "ExtractIsoYear",
    __package__,
    """The ISO-8601 week-numbering year.""",
    lookup_name="iso_year",
)


ExtractQuarter = DeclaredSubclass.make(
    Extract,
    "ExtractQuarter",
    __package__,
    """The quarter, 1 to 4.""",
    lookup_name="quarter",
)


ExtractMonth = DeclaredSubclass.make(
    Extract,
    "ExtractMonth",
    __package__,
    """The month, 1 to 12.""",
    lookup_name="month",
)


ExtractWeek = DeclaredSubclass.make(
    Extract,
    "ExtractWeek",
    __package__,
    """The ISO-8601 week number, 1 to 53.""",
    lookup_name="week",
)


ExtractWeekDay = DeclaredSubclass.make(
    Extract,
    "ExtractWeekDay",
    __package__,
    """The day of the week, 1 (Sunday) to 7 (Saturday).""",
    lookup_name="week_day",
)


ExtractIsoWeekDay = DeclaredSubclass.make(
    Extract,
    "ExtractIsoWeekDay",
    __package__,
    """The ISO-8601 day of the week, 1 (Monday) to 7 (Sunday).""",
    lookup_name="iso_week_day",
)


ExtractDay = DeclaredSubclass.make(
    Extract,
    "ExtractDay",
    __package__,
    """The day of the month.""",
    lookup_name="day",
)


ExtractHour = DeclaredSubclass.make(
    Extract,
    "ExtractHour",
    __package__,
    """The hour, 0 to 23.""",
    lookup_name="hour",
)


ExtractMinute = DeclaredSubclass.make(
    Extract,
    "ExtractMinute",
    __package__,
    """The minute, 0 to 59.""",
    lookup_name="minute",
)


ExtractSecond = DeclaredSubclass.make(
    Extract,
    "ExtractSecond",
    __package__,
    """The second, 0 to 59.""",
    lookup_name="second",
)


TruncYear = DeclaredSubclass.make(
    Trunc,
    "TruncYear",
    __package__,
    """The first moment of the year.""",
    trunc_type="year",
)


TruncQuarter = DeclaredSubclass.make(
    Trunc,
    "TruncQuarter",
    __package__,
    """The first moment of the quarter.""",
    trunc_type="quarter",
)


TruncMonth = DeclaredSubclass.make(
    Trunc,
    "TruncMonth",
    __package__,
    """The first moment of the month.""",
    trunc_type="month",
)


TruncWeek = DeclaredSubclass.make(
    Trunc,
    "TruncWeek",
    __package__,
    """The first moment of the ISO week (Monday).""",
    trunc_type="week",
)


TruncDay = DeclaredSubclass.make(
    Trunc,
    "TruncDay",
    __package__,
    """Midnight of the day.""",
    trunc_type="day",
)


TruncHour = DeclaredSubclass.make(
    Trunc,
    "TruncHour",
    __package__,
    """The start of the hour.""",
    trunc_type="hour",
)


TruncMinute = DeclaredSubclass.make(
    Trunc,
    "TruncMinute",
    __package__,
    """The start of the minute.""",
    trunc_type="minute",
)


TruncSecond = DeclaredSubclass.make(
    Trunc,
    "TruncSecond",
    __package__,
    """The start of the second.""",
    trunc_type="second",
)


TruncDate = DeclaredSubclass.make(
    Trunc,
    "TruncDate",
    __package__,
    """A datetime's date, in Hare's configured zone.""",
    trunc_type="date",
)


TruncTime = DeclaredSubclass.make(
    Trunc,
    "TruncTime",
    __package__,
    """A datetime's time of day, in Hare's configured zone.""",
    trunc_type="time",
)
