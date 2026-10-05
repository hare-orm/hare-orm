# Полнотекстовый поиск

Полнотекстовый поиск — один API на каждом диалекте, где он есть; каждый диалект пишет SQL по-своему.
В ClickHouse через hare его нет: `__search` и любое выражение поиска дают там `UnSupportedError` до
отправки SQL.

| | PostgreSQL | SQLite |
| --- | --- | --- |
| Движок | tsvector и tsquery | [FTS5](https://www.sqlite.org/fts5.html) |
| Где ищется | любая текстовая колонка или выражение | поля [`FullTextIndex`](#fulltextindex), который объявляет модель |
| `__search` | `TO_TSVECTOR(column) @@ PLAINTO_TSQUERY(text)`, у `TSVectorField` — сама колонка | `rowid IN (SELECT rowid FROM index WHERE index MATCH query)` |
| `SearchRank` | `TS_RANK()` / `TS_RANK_CD()` | `bm25()` с обратным знаком, 0 у строки, которая не подходит под запрос |
| `SearchHeadline` | `TS_HEADLINE()` | `highlight()`, а с `max_words` — `snippet()` |
| Конфигурации, лексемы, веса меток | есть | нет |
| Веса полей | через `SearchVector(weight=...)` | `SearchRank(weights={"title": 10})` |

То, что умеет только один диалект, объявляют два признака `Features`; вызов, которому нужен признак,
которого нет у соединения, даёт `UnSupportedError` до отправки SQL:

- `supports_text_search_configurations` (PostgreSQL) — конфигурация поиска (`config=`), `SearchVector`
  как самостоятельное значение (аннотация, `+`, вес), лексемы, веса меток, `normalization` и
  `cover_density` у `SearchRank`, а также `config`, `min_words`, `short_word`, `highlight_all` и
  `max_fragments` у `SearchHeadline`;
- `supports_full_text_index` (SQLite) — `FullTextIndex` и веса полей у `SearchRank`.

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

В SQLite модель объявляет индекс, по которому идёт поиск:

```python
from hare.dialects.sqlite.indexes import FullTextIndex


class Article(Model):
    title = fields.CharField(max_length=200)
    body = fields.TextField()

    class Meta:
        indexes = (FullTextIndex(fields=("title", "body"), tokenizer="porter unicode61"),)
```

## <a id="search-lookup"></a>`__search`

`field__search=text` находит строки, в тексте которых есть каждое слово `text` (`SearchType.PLAIN`);
`text` может быть и `SearchQuery`.

- PostgreSQL превращает колонку в вектор через `TO_TSVECTOR()`; `TSVectorField` берётся как есть, со
  своим `config`. У обычной текстовой колонки `field__search=SearchQuery(..., config="russian")`
  превращает колонку в вектор с `config` самого запроса.
- SQLite ищет через `FullTextIndex` модели, в который входит поле, только по колонке этого поля. Поле
  без `FullTextIndex` или аннотация, которая не является простым полем, дают `UnSupportedError` до
  отправки SQL.

## <a id="searchquery"></a>`SearchQuery`

```python
SearchQuery(value, config=None, search_type=SearchType.PLAIN, invert=False)
```

| Аргумент | Значение |
|---|---|
| `value` | Текст поиска, выражение, которое его даёт, или лексемы PostgreSQL (читаются как `RAW`) |
| `config` | Конфигурация поиска (`"english"`) — PostgreSQL |
| `search_type` | Как читается текст — `SearchType`; другое значение даёт `ConfigurationError` |
| `invert` | Отрицание запроса |

`SearchType`:

| Значение | Как читается текст | PostgreSQL | SQLite |
|---|---|---|---|
| `PLAIN` | каждое слово, в любом месте | `PLAINTO_TSQUERY` | каждое слово — термин FTS5 в кавычках |
| `PHRASE` | слова подряд, по порядку | `PHRASETO_TSQUERY` | одна фраза FTS5 |
| `WEBSEARCH` | слова, `"фразы в кавычках"`, `or` между вариантами, `-слово` для слова, которого не должно быть | `WEBSEARCH_TO_TSQUERY` | то же самое как запрос FTS5 |
| `RAW` | собственный синтаксис базы как есть | `TO_TSQUERY` | синтаксис запросов FTS5 |

В SQLite, кроме `RAW`, текст передаётся параметром и превращается в запрос FTS5 функцией, которую hare
регистрирует в своих соединениях, — ничего из него не читается как синтаксис FTS5. Текст без единого
слова не находит ни одной строки.

Запросы соединяются через `&` (подходят оба), `|` (подходит любой) и `~` (отрицание):

```python
SearchQuery("hare") | SearchQuery("rabbit")
SearchQuery("hare") & ~SearchQuery("python")
```

PostgreSQL отрицает любой запрос (`!!`). FTS5 отрицает запрос только справа от `&` — SQLite пишет
`query & ~query` как `query NOT query`; `~query` сам по себе, внутри `|` или слева от `&` даёт
`UnSupportedError` до отправки SQL.

`a & b` и `a | b` дают `CombinedSearchQuery`, который соединяет оба запроса через `SearchOperator`
(`AND`, `OR`); `SearchQueryCombinable` — база, дающая операторы обоим классам запросов. Запрос в
собственном синтаксисе лексем PostgreSQL — это `RawSearchQueryText`, который `SearchQuery` читает как
`RAW`. Все они лежат в `hare.search`.

## <a id="searchvector"></a>`SearchVector`

```python
SearchVector(*expressions, config=None, weight=None)
```

Текст для поиска из одного или нескольких полей или выражений, соединённый через пробел.

- Как вектор `SearchRank` — `SearchVector` из простых имён полей называет ранжируемые поля на каждом
  диалекте.
- Как самостоятельное значение — это tsvector PostgreSQL (`TO_TSVECTOR(...)`, `SETWEIGHT(...)` с
  `weight="A"`...`"D"`), векторы складываются через `+`. Нетекстовый источник (число, JSON) приводится к
  тексту, а `COALESCE` не даёт источнику со значением NULL сделать весь вектор NULL. Шифрованное поле
  даёт ошибку.
- `a + b` двух векторов — это `CombinedSearchVector`; `SearchVectorCombinable` — база, дающая `+`
  обоим классам векторов.

## <a id="searchrank"></a>`SearchRank`

```python
SearchRank(vector, query, weights=None, normalization=None, cover_density=False)
```

Насколько каждая строка подходит под запрос, больше — лучше; для аннотации, по которой фильтруют и
сортируют.

| Аргумент | Значение |
|---|---|
| `vector` | Ранжируемое поле, кортеж полей, `SearchVector` или (PostgreSQL) выражение tsvector — `TSVectorField` ранжируется как есть |
| `query` | Текст поиска (`PLAIN`) или `SearchQuery` |
| `weights` | Словарь весов полей — совпадение в более тяжёлом поле выше, у поля, которого нет в словаре, вес 1, каждый вес — конечное число от 0 до 1000000 (SQLite); или веса меток D, C, B, A PostgreSQL последовательностью или выражением |
| `normalization` | Битовая маска нормализации по длине текста — PostgreSQL |
| `cover_density` | `TS_RANK_CD()` вместо `TS_RANK()` — PostgreSQL |

В SQLite поля должны входить в один `FullTextIndex` модели, вес должен называть одно из его полей
(иначе `ConfigurationError`), а строка, которая не подходит под запрос, получает 0.

## <a id="searchheadline"></a>`SearchHeadline`

```python
SearchHeadline(
    expression, query, config=None, start_sel=None, stop_sel=None, max_words=None, min_words=None,
    short_word=None, highlight_all=None, max_fragments=None, fragment_delimiter=None,
)
```

Текст с отмеченными найденными словами, для аннотации.

| Аргумент | Значение |
|---|---|
| `expression` | Поле, текст которого размечается; в PostgreSQL — и выражение |
| `query` | Текст поиска (`PLAIN`) или `SearchQuery` |
| `start_sel`, `stop_sel` | Текст до и после найденного слова — по умолчанию `<b>` и `</b>`; не длиннее 1000 символов |
| `max_words` | Наибольшее число слов фрагмента — от 1; в SQLite не больше 64, и с ним используется `snippet()` FTS5 |
| `fragment_delimiter` | Текст между фрагментами — по умолчанию `" ... "` |
| `config`, `min_words`, `short_word`, `highlight_all`, `max_fragments` | Собственные параметры `TS_HEADLINE()` — PostgreSQL |

В SQLite строка, которая не подходит под запрос, получает свой текст как есть. Число вне диапазона или
метка, которая не является текстом, дают `ConfigurationError`.

## <a id="lexemes"></a>Лексемы (PostgreSQL)

```python
from hare.dialects.postgresql.search import Lexeme

SearchQuery(Lexeme("cat", prefix=True, weight="A") & ~Lexeme("dog"))
```

`Lexeme(value, invert=False, prefix=False, weight=None)` собирает tsquery из лексем, соединённых через
`&`, `|` и `~`; значение передаётся параметром. `SearchQuery` из лексем читается как `RAW`.

## <a id="fulltextindex"></a>`FullTextIndex` (SQLite)

```python
FullTextIndex(*, fields, name=None, tokenizer=None)
```

Отдельная таблица FTS5 (`CREATE VIRTUAL TABLE ... USING fts5(...)`), которая берёт текст из таблицы
модели (`content=`) и ключуется её целочисленным первичным ключом. Триггеры на вставку, обновление и
удаление держат её в согласии с таблицей — индексируется строка, записанная любым клиентом, не только
hare, — а при создании она заполняется уже имеющимися строками. Миграции создают и удаляют её
(`AddIndex`, `RemoveIndex`) вместе с триггерами; смена полей или токенизатора заменяет её.

| Аргумент | Значение |
|---|---|
| `fields` | Индексируемые текстовые поля — непустой список |
| `name` | Имя таблицы FTS5 — по умолчанию строится из таблицы и полей |
| `tokenizer` | Параметр `tokenize` FTS5: `"porter unicode61"` (основы английских слов), `"trigram"` (подстроки), ...; при None — `unicode61` самого FTS5 |

Модели нужен целочисленный первичный ключ (rowid FTS5); модель без него, пустой список полей, ключ по
убыванию или пустой токенизатор дают `ConfigurationError`. На соединении без
`Features.supports_full_text_index` создание и удаление индекса дают `UnSupportedError` до отправки SQL.

## <a id="query-plans"></a>Планы запросов

Каждое выражение поиска описывает свой [план](../../querying/query-plan-cache.ru.md): следующий запрос той же
структуры выполняется по плану со своим текстом поиска. Имена полей, тип поиска, веса полей и параметры
разметки входят в структуру.
