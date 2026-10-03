# Поиск по тексту

## Полнотекстовый поиск (`hare.dialects.postgresql.search`) {: #full-text-search }

```python
SearchVector(*expressions, config=None, weight=None)          # -> TO_TSVECTOR(...); складываются через +
SearchQuery(value, config=None, search_type=SearchType.PLAIN, invert=False)  # поддерживает | & ~
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

Для простого фильтра `field__search=` без ручной сборки `SearchVector` общий оператор `search` (см.
[Фильтры и операторы фильтра](../../querying/filters.ru.md#generic-set)) уже работает с `TSVectorField` и сам берёт его
`config`. У обычной текстовой колонки `field__search=SearchQuery(..., config="russian")` превращает
колонку в вектор с `config` самого запроса. `SearchVector` приводит нетекстовый источник (число, JSON и
т. п.) к тексту перед построением вектора, как и генерируемый `TSVectorField`.

## Сходство по триграммам и `unaccent` {: #trigram-similarity-and-unaccent }

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

В `hare.dialects.postgresql.functions.trigram` есть оценки сходства — числа с плавающей точкой от 0
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
