# Сходство по триграммам и `unaccent`

Только PostgreSQL; объявите нужные расширения в `Meta.extensions` (`("pg_trgm", "unaccent")`), и
автоматическое создание миграций их создаст.

| Оператор фильтра | Оператор SQL | Подходят строки, где |
|---|---|---|
| `trigram_similar` | `%` | текст похож на значение (`pg_trgm.similarity_threshold`) |
| `trigram_word_similar` | `%>` | значение похоже на часть текста (`pg_trgm.word_similarity_threshold`) |
| `trigram_strict_word_similar` | `%>>` | значение похоже на целые слова текста |

`unaccent` — часть пути у `CharField`/`TextField`/`CitextField`: `name__unaccent` — текст без
диакритических знаков (`unaccent()`), после которого идут текстовые операторы
(`name__unaccent__icontains`, `name__unaccent__trigram_similar`); работает также в `values()`,
`order_by()` и `F()`.

В `hare.dialects.postgresql.functions` есть оценки сходства — числа с плавающей точкой от 0
до 1:

| Функция | SQL |
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
