# SQLite: функции

В SQLite нет `DATE_TRUNC()`, статистических агрегатов и `jsonb`, а часть имеющихся функций работает по
другим правилам, чем в PostgreSQL: `UPPER()` меняет регистр только у ASCII, `CAST` отбрасывает дробную
часть. hare регистрирует собственные функции и правила сравнения (collation) на каждом открытом им
подключении SQLite, поэтому оператор фильтра или функция из `hare.query.functions` дают тот же результат,
что в PostgreSQL. Запрос обращается к ним через операторы фильтра и функции; по имени их никакой код не
вызывает.

```python
from hare.query.functions import Lower, Trunc

# hare_trunc_date_part() и hare_lower() в SQLite, DATE_TRUNC() и LOWER() в PostgreSQL.
await Book.objects.annotate(month=Trunc("published", "month"), key=Lower("title")).values("month", "key")
```

## <a id="registered-functions"></a>Что регистрирует hare

Все имена начинаются с `hare_`, кроме `regexp` и `match`, которые вызывают операторы SQLite `REGEXP` и
`MATCH`.

| Область | Функции и правила сравнения | Что дают |
|---|---|---|
| Даты и время | `hare_trunc_date_part`, `hare_extract_date_part`, `hare_date_timestamp` | `Trunc()`, `Extract()` и операторы фильтра по части даты; дата сравнивается с датой-временем по своему первому моменту. |
| Даты и время | `hare_datetime_shift`, `hare_date_shift`, `hare_datetime_difference`, `hare_date_difference` | Арифметика дат и дат-времени с `timedelta` с точностью до микросекунды — см. [Арифметика с датой и временем](../../querying/expressions.ru.md#date-time-arithmetic). |
| Даты и время | `hare_local_now` | Значение по умолчанию `Now()` у `DateField` или `TimeField` в поясе с правилами перехода на летнее время. |
| Даты и время | правило сравнения `hare_time`, `hare_time_sort_key` | `TimeField` упорядочен так, как PostgreSQL упорядочивает `TIMETZ`: по времени UTC, затем по смещению. |
| Числа | `hare_math_<name>`: `abs`, `ceil`, `floor`, `sign`, `mod`, `power`, `divisor`, `log`, `sqrt`, `exp`, `ln`, `sin`, `cos`, `tan`, `cot`, `asin`, `acos`, `atan`, `atan2`, `degrees`, `radians`, `pi` | Математические функции — см. [Математические функции](#math-functions). |
| Числа | `hare_integer_in_range` | Целый результат арифметики вне диапазона типа колонки отклоняется, как PostgreSQL отклоняет `integer out of range`. |
| Числа | `hare_greatest`, `hare_least` | `Greatest()`/`Least()` пропускают NULL; `max()`/`min()` SQLite дают NULL при любом аргументе NULL. |
| Числа | `hare_cast` | `Cast()` по правилам PostgreSQL — см. [Приведение типов](#casts). |
| Числа | `hare_stddev_pop`, `hare_stddev_samp`, `hare_var_pop`, `hare_var_samp` | `StdDev()` и `Variance()` как агрегаты и оконные функции. |
| Десятичные | правило сравнения `hare_decimal`, `hare_decimal_text`, `hare_decimal_sort_key`, `hare_decimal_overflows`, `hare_decimal_stored_text` | `DecimalField` хранится текстом, сравнивается и упорядочивается по точному значению — см. [Десятичные числа](#decimals). |
| Текст | `hare_upper`, `hare_lower` | `Upper()`, `Lower()` и операторы фильтра без учёта регистра, для любой буквы Unicode — см. [Смена регистра](#case-mapping). |
| Текст | `hare_text_<name>`: `left`, `right`, `substr`, `lpad`, `rpad`, `repeat`, `reverse`, `chr`, `ascii`, `md5`, `sha1`, `sha224`, `sha256`, `sha384`, `sha512` | Текстовые функции и хэши по правилам PostgreSQL — см. [Текстовые функции](#text-functions). |
| Текст | `hare_number_text` | Число, склеенное с текстом, даёт тот же текст, что в PostgreSQL. |
| JSON | `hare_json_*` | Операторы фильтра `JSONField`, порядок и текст значений такие же, как у `jsonb` в PostgreSQL — см. [JSON](#json). |
| Сеть | правило сравнения `hare_address` | `IPAddressField` упорядочен так, как PostgreSQL упорядочивает `inet`: IPv4 раньше IPv6, затем по байтам адреса. |
| Полнотекстовый поиск | `hare_full_text_query` | Текст поиска как запрос FTS5, слова которого никогда не читаются как синтаксис FTS5 — см. [Полнотекстовый поиск](../search-and-geodata/full-text-search.ru.md). |
| Регулярные выражения | `regexp`, `match` | `__posix_regex` и `__iposix_regex`, только на подключении с `install_regexp_functions=true` — см. [Регулярные выражения](#regular-expressions). |
| Векторы | sqlite-vec, `hare_vector_negative_inner_product` | Расстояния `VectorField`, только на подключении с `load_sqlite_vec=true` — см. [Векторный поиск](../search-and-geodata/vector-search.ru.md). |

## <a id="math-functions"></a>Математические функции

Целый аргумент даёт целое там, где его оставляет PostgreSQL (`Abs`, `Sign`, `Mod` от целых), `Mod` берёт
знак делимого, а аргумент вне области определения функции даёт исключение вместо NaN: `Ln(0)` бросает
`OperationalError` с сообщением PostgreSQL `cannot take logarithm of zero`.

## <a id="casts"></a>Приведение типов

Собственный `CAST` SQLite отбрасывает дробную часть, превращает текст, не являющийся числом, в 0 и не
знает типов дат. `Cast()` в SQLite вместо этого приводит по правилам PostgreSQL: float округляется
половиной к чётному, десятичное — половиной от нуля, текст, не являющийся значением типа, даёт
исключение, а дата или время читаются в UTC.

## <a id="decimals"></a>Десятичные числа

`DecimalField` хранится текстом, поэтому ни одна цифра не теряется в double. Правило сравнения
`hare_decimal` сравнивает этот текст по точному значению; текст, не являющийся числом, идёт после всех
чисел. `UPDATE`, задающий `DecimalField` выражением, сохраняет тот же текст, что и обычная запись
значения. `AlterField` к меньшему числу цифр точно читает хранимый текст через `hare_decimal_overflows`,
чтобы найти значение, которое больше не помещается, — см.
[`AlterField`](../../migrations/operations.ru.md#alter-field).

## <a id="case-mapping"></a>Смена регистра

`hare_upper` и `hare_lower` меняют регистр посимвольно, как PostgreSQL: символ, у которого смена регистра
даёт несколько символов (`ß` → `SS`), получает односимвольную замену или остаётся без изменений.

## <a id="text-functions"></a>Текстовые функции

Отрицательная длина `Left`/`Right` убирает символы с другого конца, `Substr` считает позиции до 1
пустыми, а `LPad`/`RPad` обрезают более длинный текст до длины. Длина, позиция и количество — 32-битные
целые, как в PostgreSQL, а результат длиннее, чем PostgreSQL допускает для текста (1 ГБ), отклоняется.

## <a id="json"></a>JSON

Функции `hare_json_*` заставляют `JSONField` вести себя как `jsonb` в PostgreSQL:

- равенство сравнивает значения JSON, а не хранимый текст: порядок ключей, пробелы и `1` против `1.0` не
  важны;
- `has_key`, `has_keys` и `has_any_keys` находят ключ объекта, строковый элемент массива или строку,
  равную ключу;
- `contains` — это `@>` у `jsonb`;
- порядок как у `jsonb`: объект > массив > логическое > число > строка > null;
- значение, прочитанное как текст, имеет вид `jsonb`: пробел после каждой `,` и `:`, ключи в порядке
  `jsonb`, число в виде точного десятичного текста;
- дата, сравниваемая со значением JSON, читается так, как PostgreSQL приводит её к `timestamp` или
  `timestamptz`.

Сами операторы фильтра — см. [`JSONField`](../../querying/filters.ru.md#jsonfield).

## <a id="regular-expressions"></a>Регулярные выражения

С `install_regexp_functions=true` в URL подключения hare регистрирует `regexp()` и `match()`, которые
вызывают операторы SQLite `REGEXP` и `MATCH`, и включает `Features.supports_posix_regex`. Шаблон
переводится из синтаксиса PostgreSQL в `re` Python, а неверный шаблон даёт исключение с сообщением
PostgreSQL. `re` Python не защищён от катастрофического перебора с возвратом: шаблон длиной всего
`(a+)+$` может занять подключение, поэтому никогда не передавайте шаблон из пользовательского ввода без
проверки.

## <a id="function-errors"></a>Ошибки

SQLite сообщает о любой ошибке зарегистрированной функции как `user-defined function raised exception`.
hare сохраняет собственное сообщение функции и бросает `OperationalError` с ним, так что ошибка читается
так же, как в PostgreSQL.

## <a id="compiled-functions"></a>Скомпилированные версии

Расширение `rust.native` (см. [Wheel-пакеты](../../getting-started/installation.ru.md#wheels)) содержит
скомпилированные версии функций и правил сравнения, которые SQLite вызывает для каждой строки. Там, где
оно установлено, hare регистрирует их вместо версий на Python; результаты те же.

## <a id="in-constraints-and-indexes"></a>В ограничениях и индексах

База проверяет ограничение и обновляет индекс без hare, а у любой другой программы, пишущей в файл, этих
функций нет. Условие ограничения или ключ индекса, SQL которого требует такой функции, при создании
бросает `UnSupportedError` с именем функции:

```python
from hare.ddl import Index
from hare.query.functions import Trunc


class Book(Model):
    published = fields.DatetimeField()

    class Meta:
        # UnSupportedError в SQLite: Trunc() требует hare_trunc_date_part.
        indexes = (Index(Trunc("published", "month"), name="book_published_month"),)
```

То же относится к `Extract()`, `Cast()`, операторам фильтра `JSONField` и регулярных выражений. Некоторые
выражения там пишутся собственным SQL SQLite:

| Выражение | В ограничении или индексе |
|---|---|
| `Upper()`, `Lower()`, операторы фильтра без учёта регистра | `UPPER()`/`LOWER()` SQLite, меняющие регистр только у букв ASCII. |
| Сравнение `DecimalField` | Сравнивается как число (`CAST(... AS NUMERIC)`), а не по точному тексту. |
| `Round()` десятичного | `ROUND()` SQLite над double. |
| Арифметика дат и времени | Бросает `ConfigurationError`: в SQLite для неё нет SQL. |
