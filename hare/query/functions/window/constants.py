#: The largest row count a window function takes - ``NTILE``'s buckets, ``LAG``/``LEAD``'s offset,
#: ``NTH_VALUE``'s row: PostgreSQL takes an ``integer`` there.
WINDOW_FUNCTION_ROW_COUNT_MAX = 2**31 - 1
