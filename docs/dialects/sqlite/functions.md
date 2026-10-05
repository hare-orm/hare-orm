# SQLite: functions

SQLite has no `DATE_TRUNC()`, no statistics aggregates and no exact decimal type. hare registers
functions of its own on every SQLite connection it opens for its functions SQLite has no SQL of, so a
function of `hare.query.functions` works. Where SQLite has a function or an operator, hare writes it and
its result is SQLite's own — `UPPER()` folds ASCII letters only, `CAST` truncates a fraction — see
[What runs differently](differences.md#results). A query reaches the registered functions through the
lookups and functions; no code calls them by name.

```python
from hare.query.functions import Lower, Trunc

# hare_trunc_date_part() and LOWER() on SQLite, DATE_TRUNC() and LOWER() on PostgreSQL.
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
| Numbers | `hare_math_<name>`: `abs`, `ceil`, `floor`, `sign`, `mod`, `power`, `log`, `sqrt`, `exp`, `ln`, `sin`, `cos`, `tan`, `cot`, `asin`, `acos`, `atan`, `atan2`, `degrees`, `radians`, `pi` | The math functions — see [Math functions](#math-functions). |
| Numbers | `hare_integer_in_range` | An integer result of arithmetic past 64 bits is refused; SQLite would make a `REAL` of it. |
| Numbers | `hare_greatest`, `hare_least` | `Greatest()`/`Least()` comparing numbers as numbers; like SQLite's `max()`/`min()`, they give NULL for any NULL argument. |
| Numbers | `hare_cast` | `Cast()` as SQLite's `CAST` converts, refusing what it would silently turn into another value — see [Casts](#casts). |
| Numbers | `hare_stddev_pop`, `hare_stddev_samp`, `hare_var_pop`, `hare_var_samp` | `StdDev()` and `Variance()` as aggregates and window functions. |
| Decimals | collation `hare_decimal`, `hare_decimal_text`, `hare_decimal_sort_key`, `hare_decimal_overflows`, `hare_decimal_stored_text`, `hare_decimal_add`, `hare_decimal_subtract`, `hare_decimal_multiply`, `hare_decimal_divide`, `hare_decimal_mod`, `hare_decimal_sum`, `hare_decimal_avg` | A `DecimalField` stored as text, compared, ordered and computed with by its exact value — see [Decimals](#decimals). |
| Text | `hare_text_<name>`: `left`, `right`, `lpad`, `rpad`, `repeat`, `reverse`, `md5`, `sha1`, `sha224`, `sha256`, `sha384`, `sha512` | The text and hash functions SQLite lacks — see [Text functions](#text-functions). |
| JSON | `hare_json_path`, `hare_json_datetime` | A path with a digit segment and a date compared with a JSON value — see [JSON](#json). |
| Full-text search | `hare_full_text_query` | The search text as an FTS5 query whose words are never read as FTS5 syntax — see [Full-text search](../search-and-geodata/full-text-search.md). |
| Regular expressions | `regexp`, `match` | `__posix_regex` and `__iposix_regex`, only on a connection with `install_regexp_functions=true` — see [Regular expressions](#regular-expressions). |
| Vectors | sqlite-vec, `hare_vector_negative_inner_product` | The `VectorField` distances, only on a connection with `load_sqlite_vec=true` — see [Vector search](../search-and-geodata/vector-search.md). |

## <a id="math-functions"></a>Math functions

SQLite's own math functions are an optional build feature, so hare registers them, and each gives what
SQLite's own gives: an integer argument gives an integer (`Abs`, `Sign`, `Mod` of integers), `Mod` takes
the sign of the dividend, an argument outside a function's domain gives `NULL` (`Ln(0)`, `Sqrt(-1)`) and
a result past a float is an infinity. A division by zero and a `Mod` by zero give `NULL` as well; hare
checks neither. `Round()` is SQLite's own rounding: half away from zero for a float.

## <a id="casts"></a>Casts

SQLite's own `CAST` truncates a fraction, turns text that isn't a number into 0 and has no date types.
`Cast()` on SQLite converts as `CAST` does and refuses only what `CAST` would silently turn into another
value:

- a fraction is cut off when a number becomes an integer, where PostgreSQL rounds;
- text that isn't a value of the type raises, and so does a `NaN`;
- an integer past the range of the target integer field (16, 32 or 64 bits) raises;
- a decimal target rounds half away from zero to its places, and a float becomes a decimal by its
  shortest digits;
- a boolean is read from `true`, `false`, `1` or `0`, in any case, trimmed;
- a date or time is read in UTC;
- a JSON null of a `JSONField` path (`Cast(F("data__n"), IntField())`) is `NULL`; a JSON string, array
  or object that isn't a value of the type raises.

A cast to text is SQLite's own: a float is `2.0`, with 15 significant digits (17 from SQLite 3.53), and a
boolean is `1` or `0`. A `CharField`'s `max_length` cuts nothing. Any other cast, such as a boolean to a
float, is SQLite's own conversion.

## <a id="decimals"></a>Decimals

A `DecimalField` is stored as text, so no digit is lost to a double. The `hare_decimal` collation
compares that text by its exact value; text that isn't a number sorts after every number. An `UPDATE`
that sets a `DecimalField` from an expression stores the same text a plain write of the value stores.
Arithmetic, `Abs`/`Ceil`/`Floor`/`Mod`, `Case`, `Coalesce`, `Sum`, `Avg` and `StdDev`/`Variance` of
Decimals are worked out from that text exactly by `hare_decimal_add`,
`hare_decimal_subtract`, `hare_decimal_multiply`, `hare_decimal_divide`, `hare_decimal_mod`,
`hare_decimal_sum` and `hare_decimal_avg`. A quotient (a division, `Avg()`, `StdDev()`, `Variance()`) is
cut, not rounded, to 20 digits after the point; PostgreSQL's numeric has scale rules of its own. `Round()`
of a decimal rounds its exact text half away from zero. `AlterField` to fewer digits reads the stored text
exactly through `hare_decimal_overflows` to find a value that no longer fits — see
[`AlterField`](../../migrations/operations.md#alter-field).

## <a id="case-mapping"></a>Case mapping

`Upper()`, `Lower()` and the case-insensitive lookups (`iexact`, `icontains`, `istartswith`,
`iendswith`) are SQLite's own `UPPER()`, `LOWER()` and `LIKE`, which fold the ASCII letters only: `é` and
`É`, `п` and `П` are different letters to them, and `Upper("привет")` is `привет`.

## <a id="text-functions"></a>Text functions

`Left`, `Right`, `LPad`, `RPad`, `Repeat`, `Reverse` and the digests are hare's functions: a negative
`Left`/`Right` length drops characters from the other end, and `LPad`/`RPad` cut a longer text to the
length. A length, a position and a count have no 32-bit limit, and a result longer than SQLite holds in
one value (1e9 bytes) is refused before it is built (`string or blob too big`).

`Substr`, `Chr` and `Ord` are SQLite's own `substr()`, `char()` and `unicode()`: a negative start counts
from the end, a negative length takes the characters before the start, and `unicode('')` is `NULL`. A text
function given an expression of the wrong type is left to SQLite; a literal of the wrong type is refused.

## <a id="json"></a>JSON

A `JSONField` is `JSON_TEXT`, and SQLite's `json_extract()`, `json_type()` and `json_object()` read it.
SQLite's JSON has no containment: `contains` and `contained_by` of a `JSONField` raise `UnSupportedError`
before any SQL is sent. hare adds the meaning of its own paths:

- a path with a digit segment is an object's key or an array's index, whichever the value at that point
  is (`hare_json_path`);
- a date compared with a JSON value is read as `timestamp` or `timestamptz`, fractional seconds cut at
  microseconds (`hare_json_datetime`).

The rest is SQLite's own:

- equality of a path compares the JSON form: `true` and `1` differ, a string and a number differ;
  equality of a whole `JSONField` compares the stored text, so key order and `1` against `1.0` matter,
  and a container in `__filter` is compared as minified JSON text;
- ordering and `gt`, `gte`, `lt`, `lte`, `range` of a path, or of a whole `JSONField`, compare the value
  `json_extract()` reads: a string as itself, `true` and `false` as 1 and 0, a JSON `null` as `NULL`, an
  object or an array as its minified text; SQLite orders numbers before text;
- `has_key`, `has_keys` and `has_any_keys` find a key of an object only (`json_type()`);
- a value read as text is SQLite's stored text, without spaces after `,` and `:`, and so is a `Cast()` to
  text and a `Concat()` of it; a text lookup of a path matches the text `json_extract()` gives;
- `JSONObject()` and `JSONArray()` are written by `json_object()`: a boolean as 1 or 0, a float with 15
  significant digits (17 from SQLite 3.53), a moment or a time as its stored text, bytes as uppercase hex, a
  decimal as a number, a JSON value nested;
- a null byte inside a JSON value is stored as it is.

See [`JSONField`](../../querying/filters.md#jsonfield) for the lookups themselves.

## <a id="regular-expressions"></a>Regular expressions

With `install_regexp_functions=true` in the connection URL, hare registers `regexp()` and `match()`,
which SQLite's `REGEXP` and `MATCH` operators call, and `Features.supports_posix_regex` is on. A pattern
is a Python `re` pattern, with its syntax and semantics: `__iposix_regex` is `re.IGNORECASE`, and
PostgreSQL's `[[:digit:]]`, `\y` or `(?n)` aren't translated. An invalid pattern raises with Python's
message. Python's `re` doesn't guard against catastrophic backtracking: a pattern as short
as `(a+)+$` can hold the connection, so never pass a pattern from user input unchecked.

## <a id="function-errors"></a>Errors

SQLite reports any error a registered function raises as `user-defined function raised exception`. hare
keeps the function's own message and raises `OperationalError` with it.

## <a id="compiled-functions"></a>Compiled versions

The `rust.native` extension (see [Wheels](../../getting-started/installation.md#wheels)) holds compiled
versions of the functions and the collation SQLite calls once per row. Where it is installed, hare registers
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

The same goes for `Extract()`, `Cast()`, the `JSONField` path lookups and the regular expression lookups.
Some expressions are written with SQLite's own SQL there instead:

| Expression | In a constraint or an index |
|---|---|
| A `DecimalField` compared | Compared as a number (`CAST(... AS NUMERIC)`), not by its exact text. |
| `Round()` of a decimal | SQLite's `ROUND()` of a double. |
| Date and time arithmetic | Raises `ConfigurationError`: SQLite has no SQL for it. |
