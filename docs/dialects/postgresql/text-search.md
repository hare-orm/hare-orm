# Text search

## Full-text search (`hare.dialects.postgresql.search`) {: #full-text-search }

```python
SearchVector(*expressions, config=None, weight=None)          # -> TO_TSVECTOR(...); supports + to combine
SearchQuery(value, config=None, search_type=SearchType.PLAIN, invert=False)  # supports | & ~
SearchRank(vector, query, weights=None, normalization=None, cover_density=False)
SearchHeadline(expression, query, config=None, start_sel=None, stop_sel=None, max_words=None, ...)
Lexeme(value, invert=False, prefix=False, weight=None)
SearchCriterion(field, expr, vectorize=True, config=None)
```

`SearchType`: `PLAIN` (`PLAINTO_TSQUERY`), `PHRASE` (`PHRASETO_TSQUERY`), `RAW` (`TO_TSQUERY`),
`WEBSEARCH` (`WEBSEARCH_TO_TSQUERY`).

```python
results = await (
    Article.objects.annotate(
        rank=SearchRank(SearchVector("title", "body", config="english"), SearchQuery("hare orm", config="english"))
    )
    .filter(rank__gt=0)
    .order_by("-rank")
)
```

For a plain `field__search=` filter without building a `SearchVector` by hand, the generic `search`
lookup (see [Filters and lookups](../../querying/filters.md#generic-set)) already works on a `TSVectorField` and
picks up its `config` automatically. On a plain text column, `field__search=SearchQuery(..., config="russian")`
vectorizes the column with the query's own `config`. `SearchVector` casts a non-text source (a number,
JSON, ...) to text before vectorizing it, and so does a generated `TSVectorField`.

## Trigram similarity and unaccent {: #trigram-similarity-and-unaccent }

Postgres only; declare the extensions they need in `Meta.extensions` (`("pg_trgm", "unaccent")`), and
the migration autodetector creates them.

| Lookup | Operator | Matches rows where |
|---|---|---|
| `trigram_similar` | `%` | the text is similar to the value (`pg_trgm.similarity_threshold`) |
| `trigram_word_similar` | `%>` | the value is similar to a part of the text (`pg_trgm.word_similarity_threshold`) |
| `trigram_strict_word_similar` | `%>>` | the value is similar to whole words of the text |

`unaccent` is a transform of a `CharField`/`TextField`/`CitextField`: `name__unaccent` is the text
without its accents (`unaccent()`), with the text lookups after it (`name__unaccent__icontains`,
`name__unaccent__trigram_similar`), also in `values()`, `order_by()` and `F()`.

`hare.dialects.postgresql.functions.trigram` has the scores, floats from 0 to 1:

| Function | SQL |
|---|---|
| `TrigramSimilarity(expression, string)` | `similarity(expression, string)` |
| `TrigramDistance(expression, string)` | `expression <-> string` |
| `TrigramWordSimilarity(string, expression)` | `word_similarity(string, expression)` |
| `TrigramWordDistance(string, expression)` | `string <<-> expression` |
| `TrigramStrictWordSimilarity(string, expression)` | `strict_word_similarity(string, expression)` |
| `TrigramStrictWordDistance(string, expression)` | `string <<<-> expression` |

```python
await Author.objects.filter(name__trigram_similar="Gerard Depardeu")
await Author.objects.filter(name__unaccent__icontains="saldana")
await Author.objects.annotate(score=TrigramSimilarity("name", "Gerard")).filter(score__gt=0.3).order_by("-score")
```
