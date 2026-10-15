# Создание диалекта: SQL

Шаги 3–6 [создания диалекта](index.ru.md): имена, литералы и параметры, типы колонок и значения,
выражения и операторы фильтра, клаузы запроса.

## <a id="dialect"></a>3. Имена, литералы и параметры: `SqlLiterals`, `SqlParameters`

```python
class ColstoreLiterals(SqlLiterals):
    identifier_quote_char = "`"
    alias_quote_char = "`"


class ColstoreParameters(SqlParameters):
    placeholder_template = "{{p{}}}"          # {p1}, {p2}, ...


class ColstoreDialect(Dialect):
    name = "colstore"
    otel_system_name = "colstore"           # db.system.name в OpenTelemetry
    features = Features(
        supports_transactions=False,
        supports_select_for_update=False,
        supports_update_limit_order_by=False,
        supports_foreign_keys=False,
        supports_unique_constraints=False,
    )

    def build_literals(self):
        return ColstoreLiterals(self)

    def build_parameters(self):
        return ColstoreParameters(self)
```

`SqlLiterals` записывает имена и значения в текст SQL; имена и литералы экранируются только здесь —
редактор схемы, отрисовка SQL и чтение схемы спрашивают его:

| Член класса | Что задаёт |
|---|---|
| `identifier_quote_char`, `alias_quote_char`, `quote_identifier(name)`, `qualify_table_name(table, schema)` | Кавычки для имён таблиц, колонок, индексов и ограничений и для псевдонимов в `SELECT`. |
| `get_string_literal_sql(text)`, `get_literal_sql(value)`, `get_boolean_literal_sql(value)`, `get_bytes_literal_sql(value)`, `get_array_literal_sql(element_sqls)` | Литералы, записываемые в текст SQL, — строка, значение колонки по умолчанию, логическое значение запроса, байты, массив. |

`SqlParameters` передаёт значения параметрами:

| Член класса | Что задаёт |
|---|---|
| `placeholder_template`, `get_placeholder(index)`, `numbers_parameters` | Место для параметра запроса; `{}` — его номер, начиная с 1 (`$1` в PostgreSQL, `?` в SQLite). Кэш вида запроса подставляет значения по позиции, поэтому с ним работает любой вид мест для параметров. |
| `get_parameter_cast_type(value, position)`, `get_field_parameter_cast_type(field)`, `get_row_key_cast_type(field)`, `get_json_object_value_cast_type(value, value_type)`, `get_cast_parameter_sql(sql, value)` | Тип, к которому приводится значение-параметр там, где его тип ничто не задаёт — см. [`get_parameter_cast_type()`](#get-parameter-cast-type) |
| `get_bindable_number(value)` | Как передаётся число, означающее число JSON. |
| `single_parameter_in_list_min_length` | Длина, начиная с которой список `__in`/`__not_in` передаётся одним параметром. |
| `get_default_rows_source_sql(row_count)`, `get_column_arrays_rows_source_sql(column_types, first_index)` | Источники, из которых многострочный `INSERT` читает строки значений по умолчанию или по одному массиву на колонку, — `None` для команды на строку и `VALUES`. |
| `get_copy_column_type(field)` | Тип, который массовой загрузке (`copy()`) называют для колонки поля, — по умолчанию тот, что объявлен в таблице. |
| `supports_copy_column_type(column_type)` | Загружает ли массовая загрузка колонку такого типа. |

### <a id="get-parameter-cast-type"></a>`get_parameter_cast_type()`

Тип, к которому приводится значение-параметр там, где его тип ничто не задаёт: по месту в запросе
(`ParameterPosition`: ветка `CASE`, выбранная или сравниваемая константа, аргумент функции), как значение
колонки, как значение ключевой колонки в строке `bulk_update()` (по умолчанию — как значение колонки),
как значение объекта JSON или в готовом SQL. По умолчанию `None` или SQL без изменений; PostgreSQL,
который определяет тип параметра только по окружению, добавляет приведение; ClickHouse приводит ключи,
потому что константа без приведения получает самый узкий тип, в который помещается.

`Dialect.sql_context` — контекст, в котором выводится каждая команда; класс запроса задаёт
`SQL_CONTEXT = dialect.sql_context`.

## <a id="types"></a>4. Типы колонок и значения: `build_types()`

`build_types()` возвращает `TypeRegistry` (`hare.dialects.base.types`), который сопоставляет классам
полей то, как диалект их хранит. Класс поля использует запись ближайшего зарегистрированного базового
класса, поэтому стороннее поле наследует хранение своего родителя:

```python
def build_types(self):
    types = TypeRegistry()
    types.register(BigIntField, TypeMapping(column_type="Int64"))
    types.register(DatetimeField, TypeMapping(column_type="DateTime64(6, 'UTC')"))
    types.register(UUIDField, TypeMapping(column_type="UUID"))
    types.register(CharField, TypeMapping(column_type=lambda field: "String"))
    types.register(
        DecimalField,
        TypeMapping(column_type=lambda field: f"Decimal({field.max_digits}, {field.decimal_places})"),
    )
    return types
```

| Член `TypeMapping` | Что задаёт |
|---|---|
| `column_type` | Тип колонки или функция `(field) -> str`. Без него — собственный `SQL_TYPE` поля. |
| `generated_sql` | Команда создания колонки первичного ключа, который генерирует база. |
| `function_cast` | Функция `(field, term) -> term`, которой колонка оборачивается везде, где её сравнивают, сортируют или копируют. |
| `to_db`, `to_lookup`, `to_python` | Заменяют `to_db_value()`, `to_lookup_value()` и `from_db_value()` поля — каждое значение передаётся и читается через подключение запроса. |
| `json_term` | Текст, которым значение колонки записывается в объект JSON, — для значения, которое хранится в виде, непригодном для JSON (16-байтовые UUID колоночного диалекта). |
| `extension` | Расширение базы, которое нужно типу колонки (`postgis` для пространственных типов PostgreSQL), — создаётся везде, где используется поле этого класса, и попадает в миграции как `CreateExtension`. |
| `naive_datetime_is_utc` | `to_python` — собственный `from_db_value()` поля, а дата-время без пояса от драйвера читается как момент в UTC. |
| `inserted_by_select` | INSERT, записывающий колонку, пишет строки как `SELECT` из них, а не `VALUES`, — для значения, записанного выражением, которое `VALUES` диалекта не читает; или `(field) -> bool`, решающая это для каждого поля. |

`Features` описывают поведение значений: `enforces_numeric_ranges` (без него hare сам проверяет диапазоны
целых и десятичных), `supports_virtual_generated_columns`. Там, где значение хранится или вычисляется
иначе, SQL дают `TermRenderers` диалекта ([следующий раздел](#functions-and-lookups)). Функция, которая
есть в базе только для части типов аргументов (`ROUND(numeric, int)` в PostgreSQL), — тоже забота
рендерера.

Пакет, добавляющий поле, может зарегистрировать его хранение на любом диалекте:

```python
DialectRegistry.get_dialect("colstore").types.register(MoneyField, TypeMapping(column_type="Decimal(18, 2)"))
```

## <a id="functions-and-lookups"></a>5. Выражения и операторы фильтров: `TermRenderers`, `FilterOperators`

По умолчанию каждое выражение выводится стандартным SQL. `build_renderers()` возвращает `TermRenderers` —
замены для того, что база записывает иначе; они находятся по иерархии классов выражения:

```python
def build_renderers(self):
    renderers = TermRenderers(self)
    renderers.register_function("LENGTH", self.render_length)         # (function, sql_context) -> sql
    renderers.register_name(functions.Coalesce, lambda term, sql_context: "ifNull")
    return renderers

@staticmethod
def render_length(length, sql_context):
    return f"lengthUTF8({length.get_arg_sql(length.args[0], sql_context)})"
```

Собственные диалекты hare строят свои рендереры на `CheckedTermRenderers`
(`hare.dialects.base.renderers`): его `add_dialect_renderers()` регистрирует рендереры диалекта,
сгруппированные по темам в классы с одним `register(renderers)` — `<Dialect>JsonRenderers`,
`<Dialect>TemporalRenderers`, `<Dialect>NumberRenderers`, `<Dialect>TextRenderers`, а также рендереры
пространственных терминов, векторов и полнотекстового поиска. При создании он проверяет, что у каждого
термина, который должен отрисовывать каждый диалект hare, — терминов JSON, дат и времени, чисел и текста,
которые ядро оставляет диалекту, — есть рендерер, и даёт `TypeError` с именами недостающих. Сторонний
диалект может начать с него так же или с обычного `TermRenderers`.

Подкласс `TermRenderers` даёт ещё и выражения, которые диалект подставляет вместо исходного там, где его
SQL нужно другое:

| Член класса | Что задаёт |
|---|---|
| `is_distinct_from_operator` | Неравенство, правильное при `NULL` (`IS NOT` в SQLite). |
| `get_decimal_compared_term()`, `get_decimal_value_term()`, `get_decimal_dividend()`, `get_assigned_decimal_term()` | Десятичные, хранящиеся текстом: при сравнении, в смеси с другими числами, при делении, при присваивании в `UPDATE`. |
| `get_enum_number_term()` | Колонка целочисленного поля-перечисления там, где с её числом считают, — операнд арифметики, аргумент числовой функции — на базе, чей тип перечисления не принимает ни того, ни другого. |
| `get_exact_integer_operand()` | Операнд целочисленной суммы, разности или произведения, которые `UPDATE` записывает в колонку, расширенный там, где арифметика базы «заворачивается», — тогда проверка записываемого значения видит точный результат. |
| `get_json_path_comparand()` | С чем сравнивается значение JSON по пути — JSON, хранящийся текстом. |
| `get_integer_aggregate_as_float()` | Среднее или статистика по целым, читаемые как число с плавающей точкой. |
| `get_datetime_part_comparand()` | Дата или время суток, сравниваемые с частью момента времени. |
| `get_concatenated_argument_sql(sql, argument)` | Аргумент склейки текста — с приведением там, где база определяет его тип только по окружению. |
| `get_ordering_term()` | По чему `ORDER BY` сортирует для выражения. |
| `get_value_range_ordering_term()` | По чему упорядочивает окно, когда его рамка — `RANGE` на число значений от текущей строки: число там, где само значение колонки не принимает смещения (десятичное, хранящееся текстом). |
| `get_composite_distinct_key()` | По чему `COUNT(DISTINCT ...)` считает составной ключ. |
| `get_never_null_column_count_argument(term)` | Что `COUNT` получает для колонки запрашиваемой таблицы, в которой нет NULL, — по умолчанию саму колонку; `*` там, где посчитать строки дешевле, чем читать колонку. |
| `get_connection_only_function(sql)` | Функция, которую hare устанавливает в своих соединениях и которую нельзя использовать в командах схемы. |

`build_filter_operators()` возвращает `FilterOperators` — соответствие между функцией сравнения, которую
задаёт оператор фильтра, и её заменой в диалекте
(`FilterOperators(self, {Lookups.is_in: colstore_is_in, ...})`, `{}` — без замен). Оператор, который
реализуют только диалекты (`DialectImplementedOperators`), должен быть заменён, иначе оператор фильтра не
поддерживается: `FilterOperators.supports_lookup()` — а с ним и `Model._meta.get_lookups(path, dialect)`
— его не включает, а фильтр с ним даёт ошибку до построения SQL.

Слишком длинный список значений, чтобы привязывать по параметру на значение, сокращает диалект: создайте
подкласс `LargeInList` (`hare.dialects.base.parameters.large_in_list`) и в `build_filter_operators()`
сопоставьте `Lookups.is_in`/`not_in`/`row_is_in`/`row_not_in` его одноимённым методам. Алгоритм написан
один раз в базовом классе — когда применяется короткая форма, как отдельно сравниваются `NULL` из списка,
как отрицается `not_in`, форма «строка значений» для составного ключа; диалект даёт только контейнер, в
который значения привязываются **одним** параметром:
`get_membership_criterion(field, values, non_null_values, element_type)` (`= ANY($1::type[])` в
PostgreSQL, `IN (SELECT value FROM json_each(?))` в SQLite) и
`get_row_container(value_rows, element_types)` для строк значений. Возврат `None` оставляет обычный
список `IN (...)`.

Рендерер меняет то, как пишется SQL терма, но никогда не то, по чему составлен ключ плана запроса, —
диалект входит в каждый ключ. Собственный класс выражения диалекта (функции массивов, триграмм и
пространственные функции PostgreSQL) объявляет в `plan_parts`, как его атрибуты входят в план, и
разрешает аргументы через `ExpressionArguments.get_result()`, как любое [своё
выражение](../custom-functions-and-expressions.ru.md#writing-a-custom-expression).

## <a id="query-class"></a>6. Части команд: `QueryClauses`

`QueryBuilder` один на все базы. Он выводит SQL по стандарту ISO и спрашивает `QueryClauses` диалекта о
каждой части, которую базы записывают по-разному; базовый `QueryClauses` пишет SQL по стандарту
(`OFFSET ... ROWS FETCH FIRST ... ROWS ONLY`) и даёт `UnSupportedError` для части, которой в стандарте
нет:

| Член класса | Что записывает |
|---|---|
| `get_statement_context(builder, sql_context)` | Контекст, в котором выводятся термы самой команды, — по умолчанию тот же; диалект, рендерерам которого нужно знать что-то о команде вокруг терма (что в ней есть группировка), возвращает свой контекст с этим знанием. |
| `get_bulk_load_statement_sql(table, columns)` | Команда, под которой массовая загрузка (`copy()`) сообщается наблюдателям, спанам и счётчику запросов. |
| `get_distinct_sql(builder, sql_context)` | `DISTINCT` / `DISTINCT ON (...)`. |
| `get_row_lock_sql(builder, sql_context)` | Блокировку строк (`select_for_update()`): `FOR UPDATE`, `NO KEY`, `OF`, `NOWAIT`, `SKIP LOCKED`. |
| `get_statement_end_sql(builder, sql_context)` | Окончание подзапроса или запроса с клаузами методов `QuerySet` диалекта после его `LIMIT` — по умолчанию пустое; ClickHouse пишет туда свои `SETTINGS` и даёт подзапросу с соединениями `join_use_nulls = 1`: иначе условие мутации выполняется без этой настройки сессии. |
| `get_main_table_suffix_sql(builder, sql_context)` | То, что идёт после первой таблицы `FROM`, — по умолчанию её выборка (`get_table_sample_sql()`); ClickHouse пишет туда `FINAL` и `SAMPLE ... OFFSET ...`. |
| `get_prewhere_sql(builder, sql_context)`, `get_limit_by_sql(builder, sql_context)` | Условие, читаемое до `WHERE`, после соединений; ограничение числа строк в группе, после `ORDER BY` — по умолчанию пустые. |
| `get_limit_offset_sql(limit_sql, offset_sql)` | Границы строк запроса. |
| `get_returning_sql(returned)` | `RETURNING` из `ReturnedValue` — SQL значения, псевдоним, под которым оно возвращается, имя обычной колонки. |
| `get_lateral_sql(subquery_sql)` | Подзапрос, присоединённый как `LATERAL`, — по умолчанию ISO `LATERAL (...)`. |
| `get_table_sample_sql(method, percent_sql, seed_sql)` | Выборку таблицы во `FROM` — по умолчанию ISO `TABLESAMPLE ... REPEATABLE (...)`. |
| `get_merge_sql(...)`, `get_merge_when_sql(when)`, `get_merge_returning_sql(returned, action_alias_sql)` | `MERGE` из готовых частей (`MergeWhenSql`) — по умолчанию ISO `MERGE`; ветка `DO NOTHING`, `WHEN NOT MATCHED BY SOURCE` и `RETURNING` там дают `UnSupportedError`. |
| `get_on_conflict_sql(builder, sql_context)` | Обработку конфликта у `INSERT`. |
| `get_update_sql(builder, sql_context)` | `UPDATE` — с чтением других таблиц во `FROM`, сортировкой и ограничением строк, если база это умеет. |
| `get_delete_sql(builder, sql_context)` | `DELETE` — по умолчанию ISO `DELETE FROM` с условиями; база, которая удаляет строки иначе (мутация `ALTER TABLE ... DELETE`), пишет свой. |
| `get_typed_placeholder_template()`, `get_values_table_columns_sql()`, `get_update_from_values_sql()`, `get_insert_rows_source_sql()` | Команды, которые hare пишет текстом: `UPDATE` из таблицы `VALUES` (`bulk_update()`), `INSERT` строк из источника. |
| `get_upsert_inserted_flag_sql()` | Выражение `RETURNING`, отличающее строку, вставленную вставкой-или-обновлением, от изменённой. |
| `get_explain_sql(sql, output_format, options, features)` | Команду `EXPLAIN`, которую используют `QuerySet.sql(explain=True)` и `explain()`. |

Класс запроса только привязывает команды к диалекту:

```python
ColstoreQuery = DeclaredSubclass.make(
    Query, "ColstoreQuery", __package__, "A query in Colstore's SQL.", SQL_CONTEXT=COLSTORE_DIALECT.sql_context
)
```

### <a id="queryset-methods"></a>Методы `QuerySet` от диалекта

Собственные модификаторы запросов базы становятся методами `QuerySet`, и hare о них знать не нужно.
Зарегистрируйте их в `install()` функцией, которая получает построитель запроса и аргументы вызова и
возвращает построитель, — например, `random_share()` колоночного тестового диалекта:

```python
def install(self):
    super().install()
    QuerySetExtensions.register("random_share", self.name, self.apply_random_share)

def apply_random_share(self, builder, percent):
    return builder.where(LiteralValue(f"abs(random()) % 100 < {int(percent)}"))
```

`Event.objects.filter(...).random_share(10)` запоминает вызов — в том числе до `Hare.init()`, — а запрос
применяет его, когда строится для своего подключения; на подключении другого диалекта это даёт
`UnSupportedError`. Пакет может зарегистрировать метод и для диалекта, который определил не он.

`QuerySetExtensions.register(name, dialect_name, apply)` (из `hare.query.queryset.extensions`) обычно
вызывается в `install()` диалекта. `apply(builder, *args, **kwargs)` возвращает построитель запроса с
применённым вызовом. Вызов запоминается в запросе (в том числе до `Hare.init()`) и переносится в
`values()`, `count()` и другие запросы, построенные из него; когда запрос строится для своего
подключения, вызов применяет реализация диалекта этого подключения. На подключении, диалект которого
такой метод не зарегистрировал, запрос даёт `UnSupportedError`, а не молча пропускает вызов. Имя, которое
у `QuerySet` уже есть, или имя, начинающееся с `_`, отклоняется с `ConfigurationError`. Имя и аргументы
каждого вызова входят в ключ плана: они вписываются в запрос после его построения и поэтому — часть
текста SQL; вызов с аргументом, который не может быть частью ключа (список, словарь), лишает запрос
плана. Аргумент, который сам описывает себя для плана, — `Q`, выражение, — входит в ключ своей
структурой, а его значения подставляются, как значения фильтров.

`register()` принимает ещё четыре именованных аргумента — для метода, которому мало построителя:

| Аргумент | Что задаёт |
|---|---|
| `reads_query=True` | `apply(builder, extension_query, *args, **kwargs)` получает и запрос — `QuerySetExtensionQuery`: его `model`, `connection`, `dialect`, `table`, признаки `is_write` и `is_summary`, а также `get_condition(q)`, `get_expression(name_or_expression)` и `join(builder, joins)`, которые разрешают условие или поле так же, как собственные фильтры запроса, и записывают их значения в его план. |
| `takes_condition=True` | Метод принимает аргументы `filter()` — объекты `Q` и фильтры, — записанные одним `Q`, который получает `apply` (`prewhere()` ClickHouse). Все диалекты, регистрирующие это имя, объявляют его одинаково, иначе `register()` даёт `ConfigurationError`. |
| `changes_rows=True` | Вызов меняет, какие строки возвращает запрос, помимо его условий (`limit_by()` ClickHouse): `count()` и `exists()` считают строки запроса как производной таблицы. |
| `read_result=` | `await read_result(extension_query, result, *args, **kwargs)` читает результат запроса строк с этим вызовом и возвращает то, что вернёт запрос (`with_totals()` ClickHouse выполняет второй оператор и возвращает строки вместе с итогами). |

`QueryBuilder.set_dialect_clause(name, value)` хранит то, что задаёт вызов, — это пишет `QueryClauses`
диалекта в `get_main_table_suffix_sql()`, `get_prewhere_sql()`, `get_limit_by_sql()` и
`get_statement_end_sql()`; их спрашивают только у запроса, где такие клаузы есть. `hare stubs` и плагин
mypy объявляют методы диалектов соединений проекта с параметрами `apply` после построителя (и запроса).
