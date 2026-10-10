# Trigram similarity and unaccent

PostgreSQL only; declare the extensions they need in `Meta.extensions` (`("pg_trgm", "unaccent")`), and
the migration autodetector creates them.

| Lookup | Operator | Matches rows where |
|---|---|---|
| `trigram_similar` | `%` | the text is similar to the value (`pg_trgm.similarity_threshold`) |
| `trigram_word_similar` | `%>` | the value is similar to a part of the text (`pg_trgm.word_similarity_threshold`) |
| `trigram_strict_word_similar` | `%>>` | the value is similar to whole words of the text |

`unaccent` is a transform of a `CharField`/`TextField`/`CitextField`: `name__unaccent` is the text
without its accents (`unaccent()`), with the text lookups after it (`name__unaccent__icontains`,
`name__unaccent__trigram_similar`), also in `values()`, `order_by()` and `F()`.

`hare.dialects.postgresql.functions` has the scores, floats from 0 to 1:

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
