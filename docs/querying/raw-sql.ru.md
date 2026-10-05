# Сырой SQL

SQL, написанный вручную, на четырёх уровнях: строки модели, прочитанные целой командой (`raw()`),
фрагмент внутри запроса ORM (`RawSQL`), команда, собранная построителем запросов (`execute_sql()`), и
результат любой команды таблицей из колонок и строк (`execute_described()`). Каждое меняющееся значение
передаётся параметром запроса.

## <a id="raw"></a>Строки модели из SQL: `raw()`

`raw()`: всё, что меняется от вызова к вызову, передавайте в `parameters` (подставляется на место
каждого `%s` в `sql` как настоящий параметр запроса), а не вставляйте в текст `sql` — см.
[`RawSQL`](#rawsql) ниже. Запрос должен выбирать
колонки первичного ключа, иначе будет `FieldError`. Выбранная колонка, которая не является ни
колонкой, ни полем модели, становится атрибутом каждого объекта, как вычисляемое значение
(`SELECT b.*, b.price * 2 AS doubled ...` даёт `book.doubled`, `COUNT(b.id) AS book_count` —
`author.book_count`), со значением, которое вернул драйвер. Имя колонки, которое встречается в
результате больше одного раза (`SELECT b.*, a.id ...`), даёт `QueryError` — задайте лишней колонке
другое имя через `AS`.

## <a id="rawsql"></a>`RawSQL`

```python
class RawSQL(ArithmeticOperators, Term):
    def __init__(self, sql: str, parameters: Sequence[Any] = ()) -> None
```

```python
await IntFields.objects.annotate(idp=RawSQL("id + 1")).order_by("-idp")
await IntFields.objects.filter(intnum__gt=0).annotate(bumped=RawSQL("intnum + %s", [10])).order_by("-bumped")
```

Всё, что меняется от вызова к вызову, передавайте в `parameters`, а не вставляйте в текст `sql`. Каждое
место `%s` в `sql` (слева направо) заменяется соответствующим элементом `parameters` как настоящий
параметр запроса — тем же способом, каким проходит любое другое значение в запросе hare, поэтому
драйвер получает его отдельно от текста SQL, а не вставленным в него. Число мест `%s` должно точно
совпадать с `len(parameters)` — это проверяется сразу при создании `RawSQL(...)`.

Параметр-список (или кортеж) передаётся одним массивом в базу, где есть параметры-массивы
(PostgreSQL): `RawSQL("SELECT unnest(%s::int[])", [ids])`, `id = ANY(%s)`. В другой базе он вызывает
`UnSupportedError`. Словарь или множество вызывают `QueryError` уже при создании `RawSQL(...)` — для
параметра JSON передайте `json.dumps(value)`, для массива — список.

Знак `%` в `sql`, который не является местом для параметра, — например, шаблон `LIKE 'prefix%'` в
PostgreSQL — нужно записывать как `%%`. Одиночный `%`, сразу за которым идёт `s`, всегда читается как
место для параметра, поэтому неэкранированный `LIKE '%stuff%'` был бы прочитан как содержащий такое
место (по начальному `%s`):

```python
await Book.objects.annotate(hit=RawSQL("title LIKE '%%dispossessed%%' AND rating >= %s", [3]))
```

`RawSQL` можно использовать и как операнд арифметики — он вставляется как SQL и никогда не
передаётся параметром, — `F("price") + RawSQL("%s", [1])`, `RawSQL('"price"') * 2`, и как значение
для `update()`: `await Book.objects.all().update(price=RawSQL('"price" + %s', [100]))`.

> [!CAUTION]
> **Никогда не вставляйте данные в текст `sql`**
>
> Всё, что находится в самом `sql`, отправляется в базу как есть, без экранирования. Собирать его
> через f-строку, `.format()` или `+` из данных, пришедших извне, — это SQL-инъекция, как и с
> готовым SQL в любом другом инструменте. Меняющееся значение всегда передавайте через `parameters`.

## <a id="execute-sql"></a>Запасной путь: построитель запросов и `execute_sql()`

`Model.objects.raw(sql, parameters)` (см. [`raw()`](#raw)) выполняет готовую строку SQL,
значения передаются через `parameters`. Для запроса, который *собирается в коде* — по-прежнему без ручного
форматирования текста SQL, — постройте его через `hare.sql.builder.queries.Query`/`Table` и выполните
через `execute_sql()`, при желании проверяя каждую строку моделью Pydantic или `TypeAdapter`:

```python
from hare.query.raw_sql import execute_sql
from hare.sql.builder import Query, Table

events = Table("event")
query = (
    Query.from_(events)
    .select(events.id, events.title)
    .where(events.status == "open")
    .orderby(events.created_at)
)

result = await execute_sql(query, schema=EventSummarySchema)
result.rows            # list[EventSummarySchema]
result.rows_affected    # int
```

`Model.get_table()` даёт собственную таблицу модели как такой `Table` — её `Meta.db_table` в её
`Meta.schema`.

```python
async def execute_sql(
    query: QueryBuilder,
    *,
    using: str | DatabaseClient | None = None,
    schema: type[SchemaT] | PydanticTypeAdapter[SchemaT] | None = None,
) -> SqlQueryResult[SchemaT] | SqlQueryResult[dict[str, Any]]
```

Без `schema` строки возвращаются обычными словарями. `using` направляет запрос на конкретное
подключение — по имени или по его клиенту; без него функция работает, только если настроено ровно одно подключение (иначе
`QueryError` со списком настроенных имён). `rows_affected` — число прочитанных строк для SELECT (или
для записи с `RETURNING`) и число строк, которые изменила сама команда, для INSERT/UPDATE/DELETE;
строки, изменённые запущенными ею триггерами или каскадами внешних ключей, не считаются — на любой
базе.

## <a id="execute-query-result"></a>Результат «сырой» команды таблицей: `execute_described()`

```python
result = await connection.execute_described(sql, values=None)
result.columns    # ("id", "name")
result.rows       # ((1, "Spring"), (2, "Autumn"))
result.row_count  # 2
```

Метод клиента любого подключения — чтобы показать результат любой команды SQL таблицей: в
SQL-консоли приложения, в отчёте. Возвращает `DescribedResult(columns, rows, row_count)` из
`hare.dialects.base.results`:

- `columns` — имена колонок в порядке `SELECT`, известные и для пустого результата; две колонки с
  одним именем остаются обе. У команды, которая не возвращает строк (`UPDATE`/`DELETE` без
  `RETURNING`, изменение схемы), колонок нет.
- `rows` — кортежи значений в порядке `columns`; пусто у команды, которая не возвращает строк.
- `row_count` — сколько строк изменила команда, для записи без `RETURNING`; иначе — сколько строк она
  вернула.

Работает на SQLite, asyncpg, rust_pg и ClickHouse, в том числе на подключении транзакции. `sql` пишется для
диалекта подключения, с его обозначениями параметров для `values`.
