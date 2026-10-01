import datetime

#: Timezone name used when none is explicitly configured.
DEFAULT_TIMEZONE = "UTC"

#: The moment whose system-local UTC offset stands in for the system zone when neither
#: `datetime.astimezone()` (before 1970 on Windows) nor the optional `tzlocal` can name it.
SYSTEM_ZONE_FALLBACK_REFERENCE_MOMENT = datetime.datetime(1970, 1, 2, tzinfo=datetime.UTC)

#: The module a package keeps its declaration-only classes in - a class there is imported by its
#: package path, as a class in a module named after it is.
DECLARATIONS_MODULE_NAME = "declarations"

#: How many parsed zones ``Timezone.parse()`` keeps.
ZONE_CACHE_SIZE = 256
