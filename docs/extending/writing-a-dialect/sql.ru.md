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
| `get_identifier_key(name)` | По чему различаются имена — два имени с одним ключом называют одну таблицу или колонку; по умолчанию имя как написано, в нижнем регистре — там, где база сравнивает имена без учёта регистра. |
| `get_string_literal_sql(text)`, `get_literal_sql(value)`, `get_boolean_literal_sql(value)`, `get_bytes_literal_sql(value)`, `get_array_literal_sql(element_sqls)` | Литералы, записываемые в текст SQL, — строка, значение колонки по умолчанию, логическое значение запроса, байты, массив. |

`SqlParameters` передаёт значения параметрами:

| Член класса | Что задаёт |
|---|---|
| `placeholder_template`, `get_placeholder(index)`, `numbers_parameters` | Место для параметра запроса; `{}` — его номер, начиная с 1 (`$1` в PostgreSQL, `?` в SQLite). Кэш вида запроса подставляет значения по позиции, поэтому с ним работает любой вид мест для параметров. |
| `get_parameter_cast_type(value, position)`, `get_field_parameter_cast_type(field)`, `get_row_key_cast_type(field)`, `get_json_object_value_cast_type(value, value_type)`, `get_cast_parameter_sql(sql, value)` | Тип, к которому приводится значение-параметр там, где его тип ничто не задаёт — см. [`get_parameter_cast_type()`](#get-parameter-cast-type) |
| `get_bindable_number(value)` | Как передаётся число, означающее число JSON. |
| `single_parameter_in_list_min_length` | Длина, начиная с которой список `__in`/`__not_in` передаётся одним параметром. |
| `get_default_rows_source_sql(row_count)`, `get_column_arrays_rows_source_sql(column_cast_types, first_parameter_index, *, column_values_sql=None)` | Источники, из которых многострочный `INSERT` читает строки значений по умолчанию или по одному массиву на колонку, — `None` для команды на строку и `VALUES`. Необязательные выражения выводят уже подготовленные массивы непосредственно в SQL; иначе источник использует placeholders параметров. |
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

Полный [справочник TypeMapping](../custom-fields.ru.md#per-dialect-storage) описывает типы колонок,
преобразователи, сортировку, поддержку NULL и расширения.

Нормализация формата драйвера выполняется в `to_python`. Например, если драйвер возвращает моменты
UTC как дату-время без пояса, преобразователь добавляет UTC перед вызовом `from_db_value()` поля.

`Features` описывают поведение значений: `divides_integers_exactly` (`/` целых даёт и дробь),
`computes_integer_statistics_exactly` (среднее целых — точное десятичное),
`supports_virtual_generated_columns`. Там, где значение хранится или вычисляется
иначе, SQL дают `TermRenderers` диалекта ([следующий раздел](#functions-and-lookups)). Функция, которая
есть в базе только для части типов аргументов (`ROUND(numeric, int)` в PostgreSQL), — тоже забота
рендерера.

Пакет, добавляющий поле, может зарегистрировать его хранение на любом диалекте:

```python
DialectRegistry.get_dialect("colstore").types.register(MoneyField, TypeMapping(column_type="Decimal(18, 2)"))
```

База, драйверы которой выводят SQL в нескольких объектах диалекта, — ClickHouse, где clickhouse-connect
пишет `JSONField` колонкой `JSON`, — называет остальные в `variants` зарегистрированного диалекта
(`ClickhouseDialect(variants=(...))`): тип, способ вывода (`register()`, `register_function()`,
`register_name()`) или оператор фильтра (`register_operator()`), зарегистрированные на нём,
регистрируются и на каждом варианте — одна регистрация доходит до всех драйверов базы. Собственные
регистрации варианта остаются на нём.

## <a id="functions-and-lookups"></a>5. Выражения и операторы фильтров: `TermRenderers`, `FilterOperators`

Обычные функции по умолчанию используют стандартную форму вызова SQL. Термам с
`requires_dialect_renderer` нужна зарегистрированная реализация. `build_renderers()` возвращает
`TermRenderers` — замены для того, что база записывает иначе; они находятся по иерархии классов терма:

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

Для стандартного вызова `register_name()` может задать только другое имя функции. Обязательный рендерер
диалекта это не заменяет. Диалекты hare строят свои рендереры на `CheckedTermRenderers`
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
| `get_distinct_from_sql(left_sql, right_sql)` | Неравенство двух значений, правильное при `NULL`, — по умолчанию `left <is_distinct_from_operator> right`. |
| `get_enum_number_term()` | Колонка целочисленного поля-перечисления там, где с её числом считают, — операнд арифметики, аргумент числовой функции — на базе, чей тип перечисления не принимает ни того, ни другого. |
| `get_same_type_math_result()` | Результат математической функции типа её аргумента (`Abs`, `Ceil`, `Floor`) — приводится обратно к типу аргумента там, где функция базы даёт другой. |
| `get_float_operand()` | Целый операнд арифметики с числом с плавающей точкой — приводится там, где в базе нет арифметики широкого целого и такого числа. |
| `get_json_path_comparand()` | С чем сравнивается значение JSON по пути — JSON, хранящийся текстом. |
| `get_statistic_as_float()` | Среднее или статистика, которые база считает числом с плавающей точкой, — читаются как оно. |
| `get_datetime_part_comparand()` | Дата или время суток, сравниваемые с частью момента времени. |
| `get_concatenated_argument_sql(sql, argument)` | Аргумент склейки текста — с приведением там, где база определяет его тип только по окружению. |
| `get_order_sql(order)` | SQL направления сортировки — `ASC`, `DESC NULLS LAST`, ... — по умолчанию его собственный текст. |
| `get_ordering_clause_sql(term, term_sql, order, sql_context)` | Один ключ `ORDER BY` — по умолчанию выражение и его направление. |
| `get_window_ordering_clause_sql(function, term, term_sql, order, sql_context)` | Один ключ `ORDER BY` окна — по умолчанию `get_window_ordering_sql()`. |
| `get_window_ordering_sql(function, term_sql, order)` | SQL одного упорядочения окна — по умолчанию выражение и его направление. |
| `get_result_branch_term(term, output_field)` | Ветвь `CASE` или `COALESCE` в том виде, какой нужен типу всего результата. |
| `get_null_if_result(term, field)` | Вызов `NULLIF` либо отказ до выполнения, если тип аргумента не может представить nullable-результат. По умолчанию возвращает терм без изменений. |
| `get_assigned_integer_term(term)` | Что `UPDATE` записывает в целочисленную колонку для выражения с дробью — с приведением там, где тип колонки её не округляет. |
| `get_assigned_enum_term(term, member_numbers)` | Что `UPDATE` записывает в колонку целочисленного перечисления для выражения — там, где колонка отклоняет число, не принадлежащее ни одному члену. |
| `get_scaled_duration(term)` | Микросекунды длительности, умноженной или делённой на число, — целые микросекунды, половина к чётному, чтобы следующая арифметика и фильтр брали прочитанное значение; по умолчанию сам терм — для базы, арифметика которой уже даёт целые микросекунды. |
| `get_duration_quotient(dividend, divisor)` | Микросекунды длительности, делённой на целое, — округлённые до ближайшего, половина — к чётному, как Python делит `timedelta`. |
| `get_composite_distinct_key()` | По чему `COUNT(DISTINCT ...)` считает составной ключ. |
| `get_never_null_column_count_argument(term)` | Что `COUNT` получает для колонки запрашиваемой таблицы, в которой нет NULL, — по умолчанию саму колонку; `*` там, где посчитать строки дешевле, чем читать колонку. |

`build_filter_operators()` возвращает `FilterOperators` — соответствие между функцией сравнения, которую
задаёт оператор фильтра, и её заменой в диалекте
(`FilterOperators(self, {Lookups.is_in: colstore_is_in, ...})`, `{}` — без замен). Оператор, который
реализуют только диалекты (`DialectImplementedOperators`), должен быть заменён, иначе оператор фильтра не
поддерживается: `FilterOperators.supports_lookup()` — а с ним и `Model._meta.get_lookups(path, dialect)`
— его не включает, а фильтр с ним даёт ошибку до построения SQL.

Замены собираются из блоков `hare.query.filters`: `Lookups` (операторы сравнения, каждый строит
`field <оператор> value`), `FieldLookups` (операторы фильтра поля по суффиксу, в том числе добавленные
`register_lookup()`), `DatePartLookups` (`__year`, `__date`, ... — часть сравнивается как своя колонка),
`JsonLookups` (операторы для всего значения JSON), `ValueEncoders` (то, что превращает значение Python
оператора в то, с чем он сравнивает, вызывается как `(value, model, field, dialect)`), `FieldLookup`
(один оператор: его условие и кодирование значения) и `Like` с `DEFAULT_LIKE_ESCAPE_CLAUSE`
(`ESCAPE '\'`) для шаблона `LIKE`.

Слишком длинный список значений, чтобы привязывать по параметру на значение, сокращает диалект: создайте
подкласс `LargeInList` (`hare.dialects.base.parameters.large_in_list`) и в `build_filter_operators()`
сопоставьте `Lookups.is_in`/`not_in`/`row_is_in`/`row_not_in` его одноимённым методам. Алгоритм написан
один раз в базовом классе — когда применяется короткая форма, как отдельно сравниваются `NULL` из списка,
как отрицается `not_in`, форма «строка значений» для составного ключа. Скалярный список использует один
параметр-контейнер через `get_membership_criterion(term, values, non_null_values, element_type)`
(`= ANY($1::type[])` в PostgreSQL, `IN (SELECT value FROM json_each(?))` в SQLite).
`get_row_container(value_rows, element_types)` задаёт форму для строк: PostgreSQL привязывает отдельный
массив на колонку. Возврат `None` оставляет обычный список `IN (...)`.

Рендерер меняет то, как пишется SQL терма, но никогда не то, по чему составлен ключ плана запроса, —
диалект входит в каждый ключ. Собственный класс выражения диалекта (функции массивов, триграмм и
пространственные функции PostgreSQL) объявляет в `plan_parts`, как его атрибуты входят в план, и
разрешает аргументы через `ExpressionArguments.get_result()`, как любое [своё
выражение](../custom-functions-and-expressions.ru.md#writing-a-custom-expression).

При определении выражений пакет задаёт неизменяемые ограничения диалекта через
`DialectExpressions.declare(MyDialect, (MyFunction, ...))` (`hare.query.expressions.dialect_expressions`).
Подклассы наследуют ограничения. Добавление во время работы использует
`DialectExpressions.register(...)`, публикующий новый снимок реестра. Запрос к другой базе отвергает каждое из
них с `UnSupportedError` до построения SQL: `Function.get_result()` проверяет это для любой функции, а
выражение со своим `get_result()` первым делом вызывает
`DialectExpressions.raise_if_dialect_lacks(self, expression_context.dialect)`.

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
| `join_keywords`, `get_join_keyword_sql(join_type)` | Ключевое слово соединения каждого типа — по умолчанию `JOIN` и `LEFT OUTER JOIN`; тип, которого нет в соответствии, даёт `UnSupportedError`. Диалект добавляет в соответствие свои соединения (`ASOF` и `ARRAY JOIN` ClickHouse). |
| `get_select_all_except_sql(quoted_columns)` | Начало запроса, выбирающего все колонки источника, кроме некоторых, — производная таблица, в которой выполняется запрос с коррелированными подзапросами там, где база выполняет их так; по умолчанию `UnSupportedError`. |
| `get_values_source_sql(row_sqls, column_names, alias_sql, *, matches_no_row)` | Подзапрос строк, заданных значениями, — источник `MERGE`. |

`get_update_from_values_sql(write=..., with_sql=..., values_sql=..., sql_context=...)` получает
декларацию `UpdateFromValues`. Присваивания содержат имена колонок без кавычек и позиции
`ValuesColumn` либо деревья выражений; сопоставления связывают имена с позициями, а фильтр остаётся
выражением. Диалект выбирает квалификацию имён и источник строк до рендеринга. Нельзя разбирать
напечатанные идентификаторы или заменять префиксы таблиц в SQL: литералы и `RawSQLTerm` неизменны.

Класс запроса только привязывает команды к диалекту:

```python
ColstoreQuery = DeclaredSubclass.make(
    Query, "ColstoreQuery", __package__, "A query in Colstore's SQL.", SQL_CONTEXT=COLSTORE_DIALECT.sql_context
)
```

### <a id="queryset-methods"></a>Методы `QuerySet` от диалекта

Методы диалекта объявляются в `get_contribution()` и публикуются вместе с диалектом.
Функция получает построитель запроса и аргументы вызова, возвращая построитель:

```python
from hare.core.extensions import ExtensionContribution, MethodRegistration
from hare.query.queryset.extensions.query_set_extension import QuerySetExtension

def get_contribution(self):
    return ExtensionContribution(methods=(
        MethodRegistration("random_share", type(self), QuerySetExtension(self.apply_random_share)),
    ))

def apply_random_share(self, builder, percent):
    return builder.where(LiteralValue(f"abs(random()) % 100 < {int(percent)}"))
```

`Event.objects.filter(...).random_share(10)` запоминает вызов — в том числе до `Hare.init()`, — а запрос
применяет его, когда строится для своего подключения; на подключении другого диалекта это даёт
`UnSupportedError`. Пакет может зарегистрировать метод и для диалекта, который определил не он.

`QuerySetExtensions.register(name, dialect_class, apply)` (из `hare.query.queryset.extensions`)
применяется вне фабрик расширений. Фабрика возвращает
`MethodRegistration(name, type(self), QuerySetExtension(apply, ...))`: метод получает каждый объект класса
диалекта.
`apply(builder, *args, **kwargs)` возвращает построитель запроса с применённым вызовом. Вызов запоминается в
запросе (в том числе до `Hare.init()`) и переносится в `values()`, `count()` и другие запросы, построенные из
него; когда запрос строится для своего подключения, вызов применяет реализация диалекта этого подключения. На
подключении, диалект которого такой метод не зарегистрировал, запрос даёт `UnSupportedError`, а не молча
пропускает вызов. Имя, которое у `QuerySet` уже есть, или имя, начинающееся с `_`, отклоняется с
`ConfigurationError`. Имя и аргументы каждого вызова входят в ключ плана: они вписываются в запрос после его
построения и поэтому — часть текста SQL; вызов с аргументом, который не может быть частью ключа (список,
словарь), лишает запрос плана. Аргумент, который сам описывает себя для плана, — `Q`, выражение, — входит в
ключ своей структурой, а его значения подставляются, как значения фильтров.

`register()` принимает дополнительные именованные аргументы, описывающие влияние на SQL и результат:

| Аргумент | Что задаёт |
|---|---|
| `reads_query=True` | `apply(builder, extension_query, *args, **kwargs)` получает и запрос — `QuerySetExtensionQuery`: его `model`, `connection`, `dialect`, `table`, признаки `is_write` и `is_summary`, а также `get_condition(q)`, `get_expression(name_or_expression)` и `join(builder, joins)`, которые разрешают условие или поле так же, как собственные фильтры запроса, и записывают их значения в его план. |
| `takes_condition=True` | Метод принимает аргументы `filter()` — объекты `Q` и фильтры, — записанные одним `Q`, который получает `apply` (`prewhere()` ClickHouse). Все диалекты, регистрирующие это имя, объявляют его одинаково, иначе `register()` даёт `ConfigurationError`. |
| `changes_rows=True` | Вызов меняет, какие строки возвращает запрос, помимо его условий (`limit_by()` ClickHouse): `count()` и `exists()` считают строки запроса как производной таблицы. |
| `transform_result=` | `transform_result(result, *args, **kwargs)` получает `QuerySetExtensionResult` после обычного выполнения. Его `result` уже материализован; запросы значений также предоставляют `statement_result` и `read_row()` для дополнительных строк драйвера, например totals. Обработчик не выполняет SQL. |
| `phase=` | `ExtensionPhase.ROW_SOURCE` (по умолчанию) применяется до оборачивания в производную таблицу; `ExtensionPhase.STATEMENT` — к итоговой команде, как настройки ClickHouse. |
| `refuses_writes=True` | Запрещает UPDATE и DELETE с этим вызовом до выбора изменяемых строк. |

Реализации и их эффект `changes_rows` выбираются для фактического соединения. Метод другого диалекта
не влияет на стратегию сводного запроса. Допускается один обработчик формы результата; сочетание
нескольких вызывает `QueryError` до выполнения SQL. Обработчик преобразует полный результат await.
`iterator()` и `stream()` выдают обычные декодированные строки и не применяют преобразование полного
результата к отдельным страницам или порциям курсора.


`QueryBuilder.set_dialect_clause(name, value)` хранит то, что задаёт вызов, — это пишет `QueryClauses`
диалекта в `get_main_table_suffix_sql()`, `get_prewhere_sql()`, `get_group_by_modifier_sql()` (то, что
идёт после `GROUP BY`, ` WITH TOTALS` ClickHouse), `get_limit_by_sql()` и `get_statement_end_sql()`; их
спрашивают только у запроса, где такие клаузы есть. `hare stubs` и плагин
mypy объявляют методы диалектов соединений проекта с параметрами `apply` после построителя (и запроса).

### <a id="query-source-bindings"></a>Привязка источников запроса

У построителя запроса, используемого как источник, есть идентичность, не зависящая от печатного alias.
`from_(subquery)` и `join(subquery)` назначают автоматические имена локальным копиям,
не изменяя переданный подзапрос. Ссылки на колонки и звёздочку используют привязку текущего
`SqlContext`. Повторное использование подзапроса в другом родителе не меняет SQL первого.
Явный `as_(...)` создаёт отдельное вхождение для соединения источника с самим собой.

Анализ корреляции обходит непосредственные области запросов. Таблицы вложенного или соседнего
запроса не становятся источниками родителя. При сравнении источников учитывается схема:
одинаковые печатные имена таблиц в разных схемах не взаимозаменяемы.

`SqlContext.features` позволяет передать возможности соединения; без них `effective_features`
использует возможности диалекта. Обычное исполнение statement и запись плана передают возможности
выбранного соединения. Поддержка корреляции в SELECT, ORDER BY, GROUP BY, UPDATE и DELETE
проверяется независимо до рендеринга statement.

### <a id="structural-sql-expressions"></a>Структура SQL-выражений

`NodeStructure` сравнивает и перестраивает SQL-выражения без рендеринга. Конкретный класс узла
объявляет семантические входы через `structure_attributes`; объявления базовых классов тоже
учитываются. Alias относится к отображению. Атрибуты с данными помечаются в `literal_attributes`:
объект выражения внутри такого значения не становится SQL-узлом для замены.
Значения словаря и элементы последовательностей могут содержать дочерние выражения; ключи
словаря остаются метками. Неизвестный подкласс непрозрачен, пока явно не объявит свои входы.
Для неповторяемого выражения задайте `has_repeatable_value=False`: два отдельных вызова нельзя объединять.

`rebuild_structure(attributes)` создаёт копию. Переопределите его, если изменение входа требует
пересчёта производных данных, например глубины составного условия. Итеративный обход отвергает
циклы предков и сохраняет неизменённые узлы. Источники и вложенные запросы ограничивают область
обхода: замена во внешнем выражении не входит в тело вложенного запроса.

`Term.replace_table()` использует тот же обход с контрактом замены таблиц. Необъявленный подкласс
сохраняет входы ближайшей объявленной базы (например, аргументы пользовательской функции), а его
дополнительные атрибуты остаются непрозрачными. Специальные реализации `replace_table()` сохраняют
свои границы. Изменяемый builder (`immutable=False`) меняется на месте; неизменённые ссылки могут
сохранить идентичность. Кэш хеша предков сбрасывается и после изменения дочернего узла на месте.
Это не раскрывает необъявленный подкласс для структурной идентичности и обычного `NodeStructure.rewrite()`.

Производные запросы используют `DerivedProjection`, которая связывает структурную идентичность
выражения с именем колонки и типом результата. Внешний запрос явно выбирает выходные псевдонимы.
Оконные фильтры и сортировка могут
запрашивать скрытые колонки, не меняя видимого результата. GROUP BY использует ту же идентичность.
Агрегации строят независимые внутреннее и внешнее состояния запроса; вхождения параметров
ссылаются на объявленные источники значений без повторения предыдущих шагов построения.
