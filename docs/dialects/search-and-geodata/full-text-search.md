# Full-text search

Full-text search is one API on every dialect with it; each dialect writes the SQL its own way. ClickHouse
has none through hare: `__search` and every search expression raise `UnSupportedError` there before any
SQL is sent.

| | PostgreSQL | SQLite |
| --- | --- | --- |
| Engine | tsvector and tsquery | [FTS5](https://www.sqlite.org/fts5.html) |
| What is searched | any text column or expression | the fields of a [`FullTextIndex`](#fulltextindex) the model declares |
| `__search` | `TO_TSVECTOR(column) @@ PLAINTO_TSQUERY(text)`, or the column as is for a `TSVectorField` | `rowid IN (SELECT rowid FROM index WHERE index MATCH query)` |
| `SearchRank` | `TS_RANK()` / `TS_RANK_CD()` | `bm25()` with the sign flipped, 0 for a row the query doesn't match |
| `SearchHeadline` | `TS_HEADLINE()` | `highlight()`, or `snippet()` with `max_words` |
| Configurations, lexemes, label weights | yes | no |
| Weights by field | through `SearchVector(weight=...)` | `SearchRank(weights={"title": 10})` |

What only one dialect runs is declared by two `Features` flags, and a call that needs a flag the
connection hasn't raises `UnSupportedError` before any SQL is sent:

- `supports_text_search_configurations` (PostgreSQL) — a text search configuration (`config=`), a
  `SearchVector` as a value of its own (an annotation, `+`, a weight), lexemes, `SearchRank`'s label
  weights, `normalization` and `cover_density`, and `SearchHeadline`'s `config`, `min_words`,
  `short_word`, `highlight_all` and `max_fragments`;
- `supports_full_text_index` (SQLite) — a `FullTextIndex` and `SearchRank`'s weights by field.

```python
from hare.search import SearchHeadline, SearchQuery, SearchRank, SearchType, SearchVector

await Article.objects.filter(body__search="hare orm")
await Article.objects.filter(body__search=SearchQuery("hare orm", search_type=SearchType.PHRASE))
await (
    Article.objects.annotate(rank=SearchRank(("title", "body"), "hare orm"))
    .filter(rank__gt=0)
    .order_by("-rank")
)
await Article.objects.annotate(excerpt=SearchHeadline("body", "hare orm", max_words=20))
```

On SQLite the model declares the index the search reads:

```python
from hare.dialects.sqlite.indexes import FullTextIndex


class Article(Model):
    title = fields.CharField(max_length=200)
    body = fields.TextField()

    class Meta:
        indexes = (FullTextIndex(fields=("title", "body"), tokenizer="porter unicode61"),)
```

## <a id="search-lookup"></a>`__search`

`field__search=text` matches the rows whose text holds every word of `text` (`SearchType.PLAIN`);
`text` can be a `SearchQuery` too.

- PostgreSQL vectorizes the column with `TO_TSVECTOR()`; a `TSVectorField` is used as it is, with its
  own `config`. On a plain text column, `field__search=SearchQuery(..., config="russian")` vectorizes the
  column with the query's `config`.
- SQLite matches through the model's `FullTextIndex` covering the field, restricted to the field's
  column. A field no `FullTextIndex` covers, or an annotation other than a plain field, raises
  `UnSupportedError` before any SQL.

## <a id="searchquery"></a>`SearchQuery`

```python
SearchQuery(value, config=None, search_type=SearchType.PLAIN, invert=False)
```

| Argument | Meaning |
|---|---|
| `value` | The search text, an expression giving it, or PostgreSQL's lexemes (read as `RAW`) |
| `config` | The text search configuration (`"english"`) — PostgreSQL |
| `search_type` | How the text is read — a `SearchType`; anything else raises `ConfigurationError` |
| `invert` | Negate the query |

`SearchType`:

| Value | Reads | PostgreSQL | SQLite |
|---|---|---|---|
| `PLAIN` | every word, anywhere | `PLAINTO_TSQUERY` | each word as a quoted FTS5 term |
| `PHRASE` | the words next to each other, in order | `PHRASETO_TSQUERY` | one FTS5 phrase |
| `WEBSEARCH` | words, `"quoted phrases"`, `or` between alternatives, `-word` for a word that must be missing | `WEBSEARCH_TO_TSQUERY` | the same, as an FTS5 query |
| `RAW` | the database's own syntax, as it is | `TO_TSQUERY` | FTS5's query syntax |

On SQLite, except for `RAW`, the text is bound as a parameter and turned into an FTS5 query by a
function hare registers on its connections — none of it is read as FTS5 syntax. Text without a word
matches no row.

Queries combine with `&` (both match), `|` (either matches) and `~` (negation):

```python
SearchQuery("hare") | SearchQuery("rabbit")
SearchQuery("hare") & ~SearchQuery("python")
```

PostgreSQL negates any query (`!!`). FTS5 negates a query only as the right side of `&` — SQLite writes
`query & ~query` as `query NOT query`; `~query` on its own, inside `|` or on the left of `&` raises
`UnSupportedError` before any SQL.

`a & b` and `a | b` give a `CombinedSearchQuery` joining the two with a `SearchOperator` (`AND`,
`OR`); `SearchQueryCombinable` is the base giving both query classes the operators. A query in
PostgreSQL's own lexeme syntax is a `RawSearchQueryText`, which a `SearchQuery` reads as `RAW`. All of
them come from `hare.search`.

## <a id="searchvector"></a>`SearchVector`

```python
SearchVector(*expressions, config=None, weight=None)
```

The searchable text of one or more fields or expressions, concatenated with a space between them.

- As the vector of `SearchRank`, a `SearchVector` of plain field names names the ranked fields on
  every dialect.
- As a value of its own it is PostgreSQL's tsvector (`TO_TSVECTOR(...)`, `SETWEIGHT(...)` with
  `weight="A"`...`"D"`), concatenated with `+`. A source that isn't text (a number, JSON) is cast to text,
  and `COALESCE` keeps a NULL source from making the vector NULL. An encrypted field raises.
- `a + b` of two vectors is a `CombinedSearchVector`; `SearchVectorCombinable` is the base giving
  both vector classes the `+`.

## <a id="searchrank"></a>`SearchRank`

```python
SearchRank(vector, query, weights=None, normalization=None, cover_density=False)
```

How well each row matches, higher for a better match — for an annotation to filter and order by.

| Argument | Meaning |
|---|---|
| `vector` | The ranked field, a tuple of fields, a `SearchVector`, or (PostgreSQL) a tsvector expression — a `TSVectorField` is ranked as it is |
| `query` | The search text (`PLAIN`) or a `SearchQuery` |
| `weights` | A dict of weights by field — a match in a heavier field ranks higher, 1 for a field left out, each a finite number from 0 to 1000000 (SQLite); or PostgreSQL's weights of the labels D, C, B, A as a sequence or an expression |
| `normalization` | PostgreSQL's length normalization bitmask |
| `cover_density` | `TS_RANK_CD()` instead of `TS_RANK()` — PostgreSQL |

On SQLite the fields have to be covered by one `FullTextIndex` of the model, a weight has to name one
of its fields (`ConfigurationError` otherwise), and a row the query doesn't match ranks 0.

## <a id="searchheadline"></a>`SearchHeadline`

```python
SearchHeadline(
    expression, query, config=None, start_sel=None, stop_sel=None, max_words=None, min_words=None,
    short_word=None, highlight_all=None, max_fragments=None, fragment_delimiter=None,
)
```

The text with the matched words marked, for an annotation.

| Argument | Meaning |
|---|---|
| `expression` | The field whose text is marked; on PostgreSQL also an expression |
| `query` | The search text (`PLAIN`) or a `SearchQuery` |
| `start_sel`, `stop_sel` | The text before and after a matched word — `<b>` and `</b>` by default; at most 1000 characters |
| `max_words` | The most words of a fragment — from 1; on SQLite at most 64, and it switches to FTS5's `snippet()` |
| `fragment_delimiter` | The text between fragments — `" ... "` by default |
| `config`, `min_words`, `short_word`, `highlight_all`, `max_fragments` | `TS_HEADLINE()`'s own options — PostgreSQL |

On SQLite a row the query doesn't match gets its text as it is. A count out of range or a marker that
isn't text raises `ConfigurationError`.

## <a id="lexemes"></a>Lexemes (PostgreSQL)

```python
from hare.dialects.postgresql.search import Lexeme

SearchQuery(Lexeme("cat", prefix=True, weight="A") & ~Lexeme("dog"))
```

`Lexeme(value, invert=False, prefix=False, weight=None)` builds a raw tsquery from lexemes combined
with `&`, `|` and `~`; the value is bound as a parameter. A `SearchQuery` of lexemes is read as `RAW`.

## <a id="fulltextindex"></a>`FullTextIndex` (SQLite)

```python
FullTextIndex(*, fields, name=None, tokenizer=None)
```

An FTS5 table of its own (`CREATE VIRTUAL TABLE ... USING fts5(...)`) reading its text from the model's
table (`content=`), keyed by the model's integer primary key. Triggers on insert, update and delete
keep it in step with the table — a row written by any client, not only hare, is indexed — and it is
filled from the rows already there when it is created. Migrations create and drop it (`AddIndex`,
`RemoveIndex`) with its triggers; changing the fields or the tokenizer replaces it.

| Argument | Meaning |
|---|---|
| `fields` | The indexed text fields — a non-empty list |
| `name` | The FTS5 table's name — generated from the table and fields by default |
| `tokenizer` | FTS5's `tokenize` option: `"porter unicode61"` (English stems), `"trigram"` (substrings), ...; FTS5's own `unicode61` when None |

The model needs an integer primary key (FTS5's rowid); a model without one, no fields, a descending
key or an empty tokenizer raise `ConfigurationError`. On a connection without
`Features.supports_full_text_index` creating or dropping the index raises `UnSupportedError` before any
SQL.

## <a id="query-plans"></a>Query plans

Every search expression describes its [plan](../../querying/query-plan-cache.md): a later query of the same
structure runs on the plan with its own search text bound. Field names, the search type, field
weights and the headline's options are part of the structure.
