# SQLite: functions

SQLite has no `DATE_TRUNC()`, no statistics aggregates and no `jsonb`, and some functions it has follow
other rules than PostgreSQL's: `UPPER()` folds ASCII only, `CAST` truncates a fraction. hare registers
functions and collations of its own on every SQLite connection it opens, so a lookup or a function of
`hare.query.functions` gives the same result as on PostgreSQL. A query reaches them through the lookups
and functions; no code calls them by name.

```python
from hare.query.functions import Lower, Trunc

# hare_trunc_date_part() and hare_lower() on SQLite, DATE_TRUNC() and LOWER() on PostgreSQL.
await Book.objects.annotate(month=Trunc("published", "month"), key=Lower("title")).values("month", "key")
```

## <a id="registered-functions"></a>What hare registers

Every name starts with `hare_`, except `regexp` and `match`, which SQLite's `REGEXP` and `MATCH`
operators call.

| Area | Functions and collations | What they give |
|---|---|---|
| Dates and times | `hare_trunc_date_part`, `hare_extract_date_part`, `hare_date_timestamp` | `Trunc()`, `Extract()` and the date part lookups; a date compared with a datetime by its first moment. |
| Dates and times | `hare_datetime_shift`, `hare_date_shift`, `hare_datetime_difference`, `hare_date_difference` | Date and datetime arithmetic with a `timedelta`, exact to the microsecond — see [Date/time arithmetic](../../querying/expressions.md#date-time-arithmetic). |
| Dates and times | `hare_local_now` | A `Now()` default of a `DateField` or `TimeField` in a zone with daylight saving rules. |
| Dates and times | collation `hare_time`, `hare_time_sort_key` | A `TimeField` ordered as PostgreSQL orders `TIMETZ`: by the UTC time, then by the offset. |
| Numbers | `hare_math_<name>`: `abs`, `ceil`, `floor`, `sign`, `mod`, `power`, `divisor`, `log`, `sqrt`, `exp`, `ln`, `sin`, `cos`, `tan`, `cot`, `asin`, `acos`, `atan`, `atan2`, `degrees`, `radians`, `pi` | The math functions — see [Math functions](#math-functions). |
| Numbers | `hare_integer_in_range` | An integer result of arithmetic out of the range of its column type is refused, as PostgreSQL refuses `integer out of range`. |
| Numbers | `hare_greatest`, `hare_least` | `Greatest()`/`Least()` skipping NULLs; SQLite's `max()`/`min()` give NULL for any NULL argument. |
| Numbers | `hare_cast` | `Cast()` with PostgreSQL's rules — see [Casts](#casts). |
| Numbers | `hare_stddev_pop`, `hare_stddev_samp`, `hare_var_pop`, `hare_var_samp` | `StdDev()` and `Variance()` as aggregates and window functions. |
| Decimals | collation `hare_decimal`, `hare_decimal_text`, `hare_decimal_sort_key`, `hare_decimal_overflows`, `hare_decimal_stored_text` | A `DecimalField` stored as text, compared and ordered by its exact value — see [Decimals](#decimals). |
| Text | `hare_upper`, `hare_lower` | `Upper()`, `Lower()` and the case-insensitive lookups, mapping every Unicode letter — see [Case mapping](#case-mapping). |
| Text | `hare_text_<name>`: `left`, `right`, `substr`, `lpad`, `rpad`, `repeat`, `reverse`, `chr`, `ascii`, `md5`, `sha1`, `sha224`, `sha256`, `sha384`, `sha512` | The text and hash functions with PostgreSQL's rules — see [Text functions](#text-functions). |
| Text | `hare_number_text` | A number concatenated to text as the text PostgreSQL gives it. |
| JSON | `hare_json_*` | The `JSONField` lookups, ordering and value text as PostgreSQL's `jsonb` has them — see [JSON](#json). |
| Network | collation `hare_address` | An `IPAddressField` ordered as PostgreSQL orders `inet`: IPv4 before IPv6, then by the address's bytes. |
| Full-text search | `hare_full_text_query` | The search text as an FTS5 query whose words are never read as FTS5 syntax — see [Full-text search](../search-and-geodata/full-text-search.md). |
| Regular expressions | `regexp`, `match` | `__posix_regex` and `__iposix_regex`, only on a connection with `install_regexp_functions=true` — see [Regular expressions](#regular-expressions). |
| Vectors | sqlite-vec, `hare_vector_negative_inner_product` | The `VectorField` distances, only on a connection with `load_sqlite_vec=true` — see [Vector search](../search-and-geodata/vector-search.md). |

## <a id="math-functions"></a>Math functions

An integer argument gives an integer where PostgreSQL keeps one (`Abs`, `Sign`, `Mod` of integers), `Mod`
takes the sign of the dividend, and an argument outside a function's domain raises instead of giving NaN:
`Ln(0)` raises `OperationalError` with PostgreSQL's message `cannot take logarithm of zero`.

## <a id="casts"></a>Casts

SQLite's own `CAST` truncates a fraction, turns text that isn't a number into 0 and has no date types.
`Cast()` on SQLite converts by PostgreSQL's rules instead: a float rounds half to even, a decimal rounds
half away from zero, text that isn't a value of the type raises, and a date or time is read in UTC.

## <a id="decimals"></a>Decimals

A `DecimalField` is stored as text, so no digit is lost to a double. The `hare_decimal` collation
compares that text by its exact value; text that isn't a number sorts after every number. An `UPDATE`
that sets a `DecimalField` from an expression stores the same text a plain write of the value stores.
`AlterField` to fewer digits reads the stored text exactly through `hare_decimal_overflows` to find a
value that no longer fits — see [`AlterField`](../../migrations/operations.md#alter-field).

## <a id="case-mapping"></a>Case mapping

`hare_upper` and `hare_lower` map character by character, as PostgreSQL does: a character whose mapping
expands to several (`ß` → `SS`) keeps its one-character mapping or stays unchanged.

## <a id="text-functions"></a>Text functions

A negative `Left`/`Right` length drops characters from the other end, `Substr` counts positions before 1
as empty, and `LPad`/`RPad` cut a longer text to the length. A length, a position and a count are 32-bit
integers, as PostgreSQL's are, and a result longer than PostgreSQL allows for a text (1 GB) is refused.

## <a id="json"></a>JSON

The `hare_json_*` functions make a `JSONField` behave as PostgreSQL's `jsonb`:

- equality compares JSON values, not stored text: key order, whitespace and `1` against `1.0` don't
  matter;
- `has_key`, `has_keys` and `has_any_keys` find a key of an object, a string element of an array or a
  string equal to the key;
- `contains` is `jsonb`'s `@>`;
- ordering follows `jsonb`: object > array > boolean > number > string > null;
- a value read as text has `jsonb`'s form: a space after each `,` and `:`, the keys in `jsonb`'s order, a
  number as its exact decimal text;
- a date compared with a JSON value is read as PostgreSQL casts it to `timestamp` or `timestamptz`.

See [`JSONField`](../../querying/filters.md#jsonfield) for the lookups themselves.

## <a id="regular-expressions"></a>Regular expressions

With `install_regexp_functions=true` in the connection URL, hare registers `regexp()` and `match()`,
which SQLite's `REGEXP` and `MATCH` operators call, and `Features.supports_posix_regex` is on. A pattern
is translated from PostgreSQL's syntax into Python's `re`, and an invalid pattern raises with
PostgreSQL's message. Python's `re` doesn't guard against catastrophic backtracking: a pattern as short
as `(a+)+$` can hold the connection, so never pass a pattern from user input unchecked.

## <a id="function-errors"></a>Errors

SQLite reports any error a registered function raises as `user-defined function raised exception`. hare
keeps the function's own message and raises `OperationalError` with it, so the error reads as it does on
PostgreSQL.

## <a id="compiled-functions"></a>Compiled versions

The `rust.native` extension (see [Wheels](../../getting-started/installation.md#wheels)) holds compiled
versions of the functions and collations SQLite calls once per row. Where it is installed, hare registers
them in place of the Python ones; the results are the same.

## <a id="in-constraints-and-indexes"></a>In constraints and indexes

The database checks a constraint and keeps an index up to date without hare, and any other program
writing the file has none of these functions. A constraint condition or an index key whose SQL needs one
raises `UnSupportedError`, naming the function, when it is created:

```python
from hare.ddl import Index
from hare.query.functions import Trunc


class Book(Model):
    published = fields.DatetimeField()

    class Meta:
        # UnSupportedError on SQLite: Trunc() needs hare_trunc_date_part.
        indexes = (Index(Trunc("published", "month"), name="book_published_month"),)
```

The same goes for `Extract()`, `Cast()`, the `JSONField` lookups and the regular expression lookups. Some
expressions are written with SQLite's own SQL there instead:

| Expression | In a constraint or an index |
|---|---|
| `Upper()`, `Lower()`, case-insensitive lookups | SQLite's `UPPER()`/`LOWER()`, which fold ASCII letters only. |
| A `DecimalField` compared | Compared as a number (`CAST(... AS NUMERIC)`), not by its exact text. |
| `Round()` of a decimal | SQLite's `ROUND()` of a double. |
| Date and time arithmetic | Raises `ConfigurationError`: SQLite has no SQL for it. |
