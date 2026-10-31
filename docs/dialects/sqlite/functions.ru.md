# SQLite: функции

В SQLite нет `DATE_TRUNC()` и статистических агрегатов. hare регистрирует
собственные функции на каждом открытом им подключении SQLite для тех своих функций, у которых в SQLite нет
SQL, поэтому работает любая функция из `hare.query.functions`. Там, где в SQLite есть функция или
оператор, hare пишет их, и результат собственный у SQLite: `UPPER()` меняет регистр только у букв ASCII,
`CAST` отбрасывает дробную часть — см. [Что работает иначе](differences.ru.md#results). Запрос обращается
к зарегистрированным функциям через операторы фильтра и функции; по имени их никакой код не вызывает.

```python
from hare.query.functions import Lower, Trunc

# hare_trunc_date_part() и LOWER() в SQLite, DATE_TRUNC() и LOWER() в PostgreSQL.
await Book.objects.annotate(month=Trunc("published", "month"), key=Lower("title")).values("month", "key")
```

## <a id="registered-functions"></a>Что регистрирует hare

Все имена начинаются с `hare_`, кроме `regexp` и `match`, которые вызывают операторы SQLite `REGEXP` и
`MATCH`.

| Область | Функции | Что дают |
|---|---|---|
| Даты и время | `hare_trunc_date_part`, `hare_extract_date_part`, `hare_date_timestamp` | `Trunc()`, `Extract()` и операторы фильтра по части даты; дата сравнивается с датой-временем по своему первому моменту. |
| Даты и время | `hare_datetime_shift`, `hare_date_shift`, `hare_datetime_difference`, `hare_date_difference` | Арифметика дат и дат-времени с `timedelta` с точностью до микросекунды — см. [Арифметика с датой и временем](../../querying/expressions.ru.md#date-time-arithmetic). |
| Даты и время | `hare_local_now` | Значение по умолчанию `Now()` у `DateField` или `TimeField` в поясе с правилами перехода на летнее время. |
| Числа | `hare_math_<name>`: `abs`, `ceil`, `floor`, `sign`, `mod`, `power`, `log`, `sqrt`, `exp`, `ln`, `sin`, `cos`, `tan`, `cot`, `asin`, `acos`, `atan`, `atan2`, `degrees`, `radians`, `pi` | Математические функции — см. [Математические функции](#math-functions). |
| Числа | `hare_greatest`, `hare_least` | `Greatest()`/`Least()` сравнивают числа как числа; как `max()`/`min()` SQLite, они дают NULL при любом аргументе NULL. |
| Даты и время | `hare_cast` | `Cast()` к дате, дате-времени или времени суток, прочитанным из хранимого текста в UTC — см. [Приведение типов](#casts). |
| Числа | `hare_stddev_pop`, `hare_stddev_samp`, `hare_var_pop`, `hare_var_samp` | `StdDev()` и `Variance()` как агрегаты и оконные функции. |
| Текст | `hare_text_<name>`: `left`, `right`, `lpad`, `rpad`, `repeat`, `reverse`, `md5`, `sha1`, `sha224`, `sha256`, `sha384`, `sha512` | Текстовые функции и хэши, которых нет в SQLite — см. [Текстовые функции](#text-functions). |
| JSON | `hare_json_path`, `hare_json_datetime` | Путь с цифровым сегментом и дата, сравниваемая со значением JSON — см. [JSON](#json). |
| Полнотекстовый поиск | `hare_full_text_query` | Текст поиска как запрос FTS5, слова которого никогда не читаются как синтаксис FTS5 — см. [Полнотекстовый поиск](../search-and-geodata/full-text-search.ru.md). |
| Регулярные выражения | `regexp`, `match` | `__posix_regex` и `__iposix_regex`, только на подключении с `install_regexp_functions=true` — см. [Регулярные выражения](#regular-expressions). |
| Векторы | sqlite-vec, `hare_vector_negative_inner_product` | Расстояния `VectorField`, только на подключении с `load_sqlite_vec=true` — см. [Векторный поиск](../search-and-geodata/vector-search.ru.md). |

## <a id="math-functions"></a>Математические функции

Собственные математические функции SQLite — необязательная часть сборки, поэтому hare регистрирует их
сама, и каждая даёт то, что дала бы функция SQLite: целый аргумент даёт целое (`Abs`, `Sign`, `Mod` от
целых), `Mod` берёт знак делимого, аргумент вне области определения даёт `NULL` (`Ln(0)`, `Sqrt(-1)`),
а результат за пределами float — бесконечность. Деление на ноль и `Mod` по нулю тоже дают `NULL`; hare
ничего из этого не проверяет. `Round()` округляет по-своему, как SQLite: float — от нуля.

## <a id="casts"></a>Приведение типов

`Cast()` к числу — собственный `CAST` SQLite, к `INTEGER` или к `REAL`:

- при переводе числа в целое дробная часть отбрасывается, тогда как PostgreSQL округляет;
- текст, не являющийся числом, — `0`, число, обрамлённое пробелами, — его число (`' 42 '` — это `42`);
- целое сохраняется, каким бы ни был диапазон целевого поля.

Логического типа в SQLite нет: приведение к логическому читает значение как число
(`CAST(... AS NUMERIC)`), истинное, если оно не `0`, — `'true'` даёт ложь. Типов дат в SQLite тоже нет:
дату или время читает из хранимого текста в UTC функция hare `hare_cast`. JSON null пути `JSONField`
(`Cast(F("data__n"), IntField())`) даёт `NULL`.

Приведение к тексту собственное для SQLite: float даёт `2.0` с 15 значащими цифрами (с SQLite 3.53 — с 17),
логическое — `1` или `0`. `max_length` у `CharField` ничего не обрезает. Любое другое приведение, например
логического к float, — собственное преобразование SQLite.

## <a id="case-mapping"></a>Смена регистра

`Upper()`, `Lower()` и операторы фильтра без учёта регистра (`iexact`, `icontains`, `istartswith`,
`iendswith`) — собственные `UPPER()`, `LOWER()` и `LIKE` SQLite, которые меняют регистр только у букв
ASCII: `é` и `É`, `п` и `П` для них разные буквы, а `Upper("привет")` — это `привет`.

## <a id="text-functions"></a>Текстовые функции

`Left`, `Right`, `LPad`, `RPad`, `Repeat`, `Reverse` и хэши — собственные функции hare: отрицательная
длина `Left`/`Right` убирает символы с другого конца, а `LPad`/`RPad` обрезают более длинный текст до
длины. У длины, позиции и количества нет 32-битного предела, а результат длиннее того, что SQLite держит
в одном значении (1e9 байт), отклоняется до построения (`string or blob too big`).

`Substr`, `Chr` и `Ord` — собственные `substr()`, `char()` и `unicode()` SQLite: отрицательное начало
считается с конца, отрицательная длина берёт символы перед началом, а `unicode('')` — `NULL`. Текстовая
функция, получившая выражение не того типа, оставляется на усмотрение SQLite; литерал не того типа
отклоняется.

## <a id="json"></a>JSON

`JSONField` — это `JSON_TEXT`, его читают `json_extract()`, `json_type()` и `json_object()` SQLite.
В JSON SQLite нет вхождения: `contains` и `contained_by` у `JSONField` дают `UnSupportedError` до отправки
SQL. hare добавляет смысл собственных путей:

- путь с цифровым сегментом — это ключ объекта или индекс массива, смотря что лежит в этом месте
  (`hare_json_path`);
- дата, сравниваемая со значением JSON, читается как `timestamp` или `timestamptz`, доли секунды
  отсекаются до микросекунд (`hare_json_datetime`).

Остальное — собственное поведение SQLite:

- равенство пути сравнивает форму JSON: `true` и `1` различаются, строка и число различаются; равенство
  всего `JSONField` сравнивает хранимый текст, поэтому порядок ключей и `1` против `1.0` важны, а контейнер
  в `__filter` сравнивается как сжатый текст JSON;
- порядок и `gt`, `gte`, `lt`, `lte`, `range` пути или всего `JSONField` сравнивают значение, которое
  читает `json_extract()`: строку как есть, `true` и `false` как 1 и 0, JSON `null` как `NULL`, объект или
  массив как сжатый текст; SQLite ставит числа раньше текста;
- `has_key`, `has_keys` и `has_any_keys` находят только ключ объекта (`json_type()`);
- значение, прочитанное как текст, — это хранимый текст SQLite без пробелов после `,` и `:`; таков же
  `Cast()` в текст и `Concat()` от него; текстовый оператор фильтра по пути сопоставляет текст, который
  даёт `json_extract()`;
- `JSONObject()` и `JSONArray()` пишет `json_object()`: логическое как 1 или 0, float с 15 значащими цифрами
  (с SQLite 3.53 — с 17), момент или время как хранимый текст, байты как шестнадцатеричная запись заглавными,
  значение JSON вкладывается;
- нулевой байт внутри значения JSON хранится как есть.

Сами операторы фильтра — см. [`JSONField`](../../querying/filters.ru.md#jsonfield).

## <a id="regular-expressions"></a>Регулярные выражения

С `install_regexp_functions=true` в URL подключения hare регистрирует `regexp()` и `match()`, которые
вызывают операторы SQLite `REGEXP` и `MATCH`, и включает `Features.supports_posix_regex`. Шаблон
— это выражение `re` Python с его синтаксисом и семантикой: `__iposix_regex` — это `re.IGNORECASE`, а
`[[:digit:]]`, `\y` или `(?n)` из PostgreSQL не переводятся. Неверный шаблон даёт исключение с сообщением
Python. `re` Python не защищён от катастрофического перебора с возвратом: шаблон длиной всего
`(a+)+$` может занять подключение, поэтому никогда не передавайте шаблон из пользовательского ввода без
проверки.

## <a id="function-errors"></a>Ошибки

SQLite сообщает о любой ошибке зарегистрированной функции как `user-defined function raised exception`.
hare сохраняет собственное сообщение функции и бросает `OperationalError` с ним.

## <a id="compiled-functions"></a>Скомпилированные версии

Расширение `rust.native` (см. [Wheel-пакеты](../../getting-started/installation.ru.md#wheels)) содержит
скомпилированные версии функций, которые SQLite вызывает для каждой строки. Там, где
оно установлено, hare регистрирует их вместо версий на Python; результаты те же.

## <a id="in-constraints-and-indexes"></a>В ограничениях и индексах

База проверяет ограничение и обновляет индекс без hare, а у любой другой программы, пишущей в файл, этих
функций нет. Условие ограничения, ключ индекса или выражение `GeneratedField`, SQL которых требует такой
функции (`Greatest()` — это `hare_greatest`), бросают `UnSupportedError` с именем функции при привязке
модели (`Hare.init()`) — раньше, чем `makemigrations` запишет миграцию, которую не применит ни один
`migrate`:

```python
from hare.ddl import Index
from hare.query.functions import Trunc


class Book(Model):
    published = fields.DatetimeField()

    class Meta:
        # UnSupportedError в SQLite: Trunc() требует hare_trunc_date_part.
        indexes = (Index(Trunc("published", "month"), name="book_published_month"),)
```

То же относится к `Extract()`, `Cast()`, операторам фильтра путей `JSONField` и регулярных выражений.
Арифметика дат и времени там бросает `ConfigurationError`: в SQLite для неё нет SQL.
