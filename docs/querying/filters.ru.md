# Фильтры и операторы фильтра

Фильтр — именованный аргумент `field__lookup=value` у `filter()`, `exclude()`, `get()` или `Q()`: путь
называет поле, при необходимости через связи, а оператор фильтра в его конце говорит, как сравнивать
значение. Здесь перечислены операторы фильтра, которые есть у каждого поля, и те, что добавляет тип поля;
как фильтровать по связям, `FilteredRelation`, `Lateral`, `AsofJoin`, `JsonTable` и полям JSON; как
сравнивается `NULL`.

Значение берётся таким, какое оно в момент вызова: список или множество, переданные в `filter()`,
`exclude()` или `Q()`, копируются, а словарь — целиком, поэтому его изменение до выполнения запроса запрос
не меняет; так же и значения `update()`, где и списки копируются целиком.

## <a id="generic-set"></a>Общий набор каждого обычного поля

Регистрируется для **каждого** поля самой модели, какой бы ни был его тип в Python:

`exact` (без окончания), `not`, `in`, `not_in`, `isnull`, `not_isnull`, `gte`, `lte`, `gt`, `lt`, `range`
(ровно 2 значения), `contains`, `startswith`, `endswith`, `iexact`, `icontains`, `istartswith`,
`iendswith`, `search`, `posix_regex`, `iposix_regex`, а в PostgreSQL ещё `trigram_similar`,
`trigram_word_similar`, `trigram_strict_word_similar` (см. [Сходство по триграммам и
`unaccent`](../dialects/postgresql/trigrams.ru.md)).

`CharField`/`TextField`/`CitextField` в PostgreSQL принимают ещё часть пути `unaccent`:
`filter(name__unaccent__icontains="saldana")` сравнивает текст без диакритических знаков.

`DateField`/`TimeField`/`DatetimeField` получают также свои операторы по частям даты и времени — см.
[Операторы по частям даты и времени](#date-part-lookups) ниже; у полей других типов их нет (`FieldError`,
как для любого другого неизвестного окончания).

Ключ с пустой частью — `name__`, `__name`, `author____name` — даёт `FieldError` на любом диалекте: в
`filter()`, `exclude()`, `distinct()` и `group_by()`, как и в `order_by()` и `values()`.

Все варианты `contains`/`startswith`/`endswith`/`iexact`/`i*` и `posix_regex`/`iposix_regex` сначала
приводят левую часть к тексту — формально их можно вызвать и для числового поля, хотя это редко нужно;
`float`, десятичное или логическое значение сопоставляется с собственным текстом значения в базе
(`CAST ... AS VARCHAR`): `float` — `2` в PostgreSQL и `2.0` в SQLite, логическое значение —
`true`/`false` в PostgreSQL и `1`/`0` в SQLite. `CitextField` остаётся `citext`, поэтому эти операторы
сравнивают без учёта регистра (и `posix_regex` тоже).

`contains`/`startswith`/`endswith` и варианты `i...` принимают и колонку или выражение —
`filter(body__icontains=F("title"))`, `filter(path__startswith=Concat(F("root"), Value("/")))`: образец
собирается в SQL из текста значения, а его `%`, `_` и `\` сравниваются буквально, как в строковом
значении. Строка, у которой значение — NULL, не подходит ни под один из них.

```python
await Book.objects.filter(title__icontains="dispossessed")
await Book.objects.filter(published_at__year=1974)
await Author.objects.filter(created_at__range=(start, end))
await Author.objects.filter(created_at__range=(start, None))   # без верхней границы: created_at >= start
await Author.objects.filter(created_at__range=(None, None))    # каждая строка без NULL
```

`range` принимает `None` на месте любой границы (или обеих) — тогда проверка идёт без этой границы вместо
полного `BETWEEN`; передавайте кортеж как есть, отдельно обрабатывать `None` не нужно. `in`/`not_in`
принимают любой итерируемый объект — список, кортеж, множество, `frozenset`, `range`, генератор,
`dict.keys()`/`.values()` — и читают его ровно один раз; строка или байты дают `UnSupportedError`, а не
сравниваются по символам. `field=None` и `field__iexact=None` означают `field__isnull=True`, а
`field__not=None` — `field__isnull=False`. `field__not=value` оставляет строку со значением NULL, как
`exclude(field=value)` (см. [Как работает NULL](#null-semantics)). `isnull`/`not_isnull` требуют
настоящий `bool` — строка (например, неразобранный параметр запроса) даёт `UnSupportedError`, а не
проверку на истинность.

`posix_regex`/`iposix_regex` требуют подключения с регулярными выражениями
(`features.supports_posix_regex`): это любое подключение PostgreSQL и подключение SQLite, в адресе
которого есть `?install_regexp_functions=true`. На любом другом подключении фильтр с ними даёт
`UnSupportedError` до выполнения запроса.

В SQLite шаблон — это шаблон Python `re`, с синтаксисом и семантикой `re`: синтаксис регулярных
выражений PostgreSQL не переводится (`[[:digit:]]`, `\y` и `(?n)` там недоступны), а `iposix_regex` —
это `re.IGNORECASE`. Неверный шаблон даёт `OperationalError` с сообщением Python
(`invalid regular expression: ...`).

> [!WARNING]
> **`posix_regex`/`iposix_regex` в SQLite: никогда не передавайте непроверенный ввод**
>
> PostgreSQL выполняет их своими операторами `~`/`~*`, а в SQLite своего движка регулярных выражений нет
> — там hare использует `re.search()` из Python. Модуль `re` в Python не защищён от катастрофического
> перебора с возвратом: неудачный шаблон может «повесить» всё соединение на данных обычной длины, каким
> бы коротким ни был сам шаблон (ReDoS). Для шаблона, который написали вы сами, это нормально; шаблон от
> пользователя этому оператору в SQLite не передавайте никогда.
> Ограничивайте длину и содержимое любого шаблона, который писали не вы, или выполняйте такой поиск в
> PostgreSQL.

`iexact`/`icontains`/`istartswith`/`iendswith` и `Upper()`/`Lower()` меняют регистр так, как это делает
база: PostgreSQL — по своим правилам, SQLite — своими `UPPER()`/`LOWER()` и `LIKE`, которые меняют
регистр только у букв ASCII (`iexact="ПРИВЕТ"` не находит `привет`), ClickHouse — через
`upperUTF8()`/`lowerUTF8()`.

## <a id="date-part-lookups"></a>Операторы по частям даты и времени

`DateField`, `TimeField` и `DatetimeField` получают только те части, которые есть у того, что они хранят:

| Поле | Части |
|---|---|
| `DateField` | `year`, `iso_year`, `quarter`, `month`, `week`, `week_day`, `iso_week_day`, `day` |
| `TimeField` | `hour`, `minute`, `second`, `microsecond` |
| `DatetimeField` | все двенадцать, а также `date` и `time` |

`week` и `iso_year` следуют ISO-8601, `week_day` считает от 1 (воскресенье) до 7 (суббота), а
`iso_week_day` — от 1 (понедельник) до 7. `date`/`time` у `DatetimeField` сравнивают его дату или время
суток (значение `date`/`time` или его текст ISO). После каждой части можно добавить сравнение: `not`,
`gt`, `gte`, `lt`, `lte`, `in`, `not_in` и `range` (`published_at__year__gte=2020`,
`starts_at__date__range=(start, end)`, `created__week_day__in=[1, 7]`). Значение части — `int`: другой
тип даёт `UnSupportedError`, а int за пределами 32 бит, которых не достигает ни одна часть даты, —
`ValidationError`.

Часть, которой у поля нет (`a_date_field__hour=...`, `a_time_field__year=...`), не зарегистрирована вовсе
— `FieldError`, как для любого другого неизвестного окончания, а не бессмысленное извлечение в базе. У
полей, которые не хранят дату или время, этих частей тоже нет.

```python
await Book.objects.filter(published_at__year=1974)
await Book.objects.filter(published_at__week=3)
await Loan.objects.filter(lent_at__hour=9)
await Loan.objects.filter(lent_at__date=date(2026, 1, 5), lent_at__hour__lt=12)
```

### <a id="time-zone-semantics-on-datetimefield"></a>Часовые пояса у `DatetimeField`

Части `DatetimeField` берутся из **времени на часах, которое сам hare записывает и читает** для поля, а
не обязательно в том поясе, в котором база показывает значения в сеансе:

- **`use_timezone=True`**: в настроенном поясе `timezone` (`Hare.init(timezone=...)`) — в том же, в
  который `DatetimeField.from_db_value()` переводит каждое прочитанное значение.
- **`use_timezone=False`**: во времени на часах в том виде, в котором оно было записано, — в том же
  значении без пояса, которое проходят туда и обратно `DatetimeField.to_db_value()`/`from_db_value()`. В
  SQLite это получается само (колонка хранит этот же текст без пояса, переводить нечего). В
  **PostgreSQL** колонка и при `use_timezone=False` остаётся `TIMESTAMPTZ`, а сам PostgreSQL иначе всегда
  берёт части в поясе своего сеанса, что молча расходится с местным временем, которым пользовался
  записывающий процесс, когда эти пояса различаются. hare поправляет это, указывая при извлечении
  **местный пояс системы** (`AT TIME ZONE <IANA zone>`), а не постоянное смещение: постоянное смещение
  ошиблось бы для даты по другую сторону перехода на летнее время.

  Чтобы узнать имя местного пояса IANA, нужен необязательный пакет `tzlocal`:

  ```bash
  pip install hare-orm[tzlocal]
  ```

  Без него при `use_timezone=False` то, что должно назвать пояс машины, даёт `ConfigurationError` с
  подсказкой по установке: в PostgreSQL — оператор по части даты у `DatetimeField`; в ClickHouse —
  оператор по части даты, `Trunc()`/`Extract()` значения без пояса и `Now()` для `DateField`/`TimeField`.
  Больше `tzlocal` ничему не нужен — в том числе при `use_timezone=True` и в SQLite.

Чтобы брать части в другом поясе для блока кода — например, в поясе пользователя, — оберните его в
`Timezone.override()` (`from hare.time import Timezone`):

```python
with Timezone.override("Europe/Moscow"):
    loans_today = await Loan.objects.filter(lent_at__date=moscow_today).count()
```

Внутри блока (и в задачах, которые он запускает) `Timezone.name()` возвращает этот пояс, поэтому
операторы по частям даты и времени (`__date`, `__year`, `__hour`, ...), `Extract*` и `Trunc*` берут части
в нём, а дата-время без пояса читается в нём же; значения даты-времени по-прежнему возвращаются как те же
моменты. Пояс задаётся именем IANA или объектом `ZoneInfo` — неизвестный пояс или постоянное смещение без
имени IANA дают `ConfigurationError`. Отдельное выражение получает свой пояс через `tzinfo=` — см.
[Функции даты](functions.ru.md#date-functions).

## <a id="forward-foreignkeyfield-onetoonefield"></a>Прямые `ForeignKeyField` / `OneToOneField`

Оператор по имени самой связи работает с её колонкой ключа: `author__isnull=True`, `author__in=[author1,
author2]` (объекты или значения ключа вперемешку), `author__not_in=[...]`, `author__gt=5`,
`author__in=Author.objects.filter(...)` — в `filter()`, `exclude()`, `Q` и `When`, так же, как
`author_id__...`. Объект заменяется значением своего `to_field` (первичного ключа, если у связи не задан
`to_field=`); несохранённый объект или объект другой модели — и конкретного подкласса связанной модели,
строки которого лежат в другой таблице, — дают `QueryError` (и у связи «ко многим»), как и такой оператор
у связи с составным ключом (фильтруйте по каждой колонке ключа отдельно). Имя поля связанной модели
(`author__name`) по-прежнему фильтрует через соединение таблиц.

## <a id="large-in-lists"></a>Длинные списки `__in`

Длинный список `__in`/`__not_in` передаётся одним параметром, поэтому его длина не ограничена
ограничением драйвера на число параметров: PostgreSQL использует `= ANY($1::type[])` начиная с 20
значений, SQLite — `IN (SELECT value FROM json_each(?))` начиная с 20 значений
(`SQLITE_IN_JSON_ARRAY_THRESHOLD`), с теми же правилами сравнения, что и обычный `IN (...)`, для целых
чисел, текста, чисел с плавающей точкой, `Decimal`, дат, даты-времени, `UUID`, `bool` и `bytes`. Такой
список держит один план запроса при любой длине. Так же работают операторы «многие-ко-многим» и обратного
внешнего ключа (`tags__in=`, `children__not_in=`) и `__in` у вычисляемого значения: значение без
известного типа поля (`RawSQL`, `Count()` в сравнении в `HAVING`) в PostgreSQL получает тип по своим
значениям — целые как `BIGINT`, текст как `TEXT` и так далее; список со значениями разных типов остаётся
в обычном виде. `Length()` и `ExtractYear()`/`ExtractMonth()`/`ExtractDay()` — целые числа, а
`StringAgg()` — текст, каким бы ни был их аргумент, поэтому `__in` по ним передаёт целые числа или текст.

Составной первичный ключ фильтруется через `pk` так же, как ключ из одной колонки: `pk=(1, 2)`, `pk__in=`
со списком кортежей, запросом, `values_list("pk", flat=True)` или объединением
(`(a, b) IN (SELECT ...)`), `pk__not=` и `pk__not_in=` — их отрицание, а связь с ним сравнивает весь ключ
— `bay=obj`, `bay=(1, 2)`, `bay__in=`, `bay__not`, `bay__isnull` (и то же через `bay__pk`: `bay__pk__not`
и `bay__pk__not_in` оставляют строку без `bay`, как `bay__not`); `update(bay=obj)` (или кортеж, или
`None`) записывает каждую колонку ключа. `pk__in=[(a, b), ...]` у составного первичного ключа — одна
проверка строки значений, `(a, b) IN ((1, 2), ...)`; длинный список передаётся одним массивом на колонку
в PostgreSQL (`IN (SELECT * FROM unnest($1::int[], $2::int[]))`, начиная с 20 строк) и одним массивом
JSON строк в SQLite (`IN (SELECT json_extract(value, '$[0]'), ... FROM json_each(?))`, начиная с 20
значений), поэтому его длина тоже не ограничена. Список ключей связи, где у ключа есть часть `None`
(`(None, None)` у связи, допускающей NULL) или выражение, сравнивается по ключу — условие на ключ,
соединённые через `OR`, — и его значения передаются по одному: длинный такой список — длинная команда,
которую база, ограничивающая глубину выражения (SQLite), может отвергнуть. В списке значений составной
связи указывайте все компоненты: `(None, None)` для NULL-ключа из двух колонок; отдельный `None` даёт
`QueryError`. `__in`/`__not_in` связи
«многие-ко-многим» с составным ключом работают так же. `add()`/`remove()` связи «многие-ко-многим» и
проверка `on_delete=PROTECT` при массовом удалении тоже справляются с любым числом объектов —
`add()` делит свой `INSERT` на столько
команд, сколько требует ограничение на число параметров, в одной транзакции.

## <a id="manytomanyfield"></a>`ManyToManyField`

`exact`, `not`, `in`, `not_in` (все — через промежуточную таблицу; для связанной модели с составным
первичным ключом каждое значение — кортеж ключа или объект). `isnull`/`not_isnull` доступны всегда, в
`filter()`, `exclude()` и `Q`: `tags__isnull=True` находит строку без связанных строк,
`tags__isnull=False` — строку хотя бы с одной: это проверка `EXISTS`, и каждая строка приходит один раз,
в отличие от `tags__id__isnull=False` или `books__isnull=False`, которые присоединяют связанные строки.
Связь учитывается, только если и связанная строка, и (для `through=Model`) промежуточная строка проходят
собственные ограничения мягкого удаления и арендатора — так же, как оператор по обратному внешнему ключу
пропускает мягко удалённую связанную строку.

## <a id="backward-fk-relations"></a>Обратные внешние ключи

`exact`, `not`, `in`, `not_in`, `isnull`, `not_isnull`. Для модели-владельца с составным первичным ключом
значение — объект или кортеж его ключа (`cemps=obj`, `cemps__in=[obj, (1, 2)]`), сравнение идёт по
колонкам.

`relation=None` у связи «ко многим» («многие-ко-многим» или обратный внешний ключ) — это
`relation__isnull=True`, строки без связанных строк; `relation__not=None` — это `relation__isnull=False`.

Оператор по имени самой связи «ко многим» (`tags=tag`, `tags__in=[...]`, `tags__not=...`,
`books__isnull=...`) — это тот же оператор по первичному ключу связанной модели (`tags__id=tag.id`).
Внутри одного вызова `.filter()` он делит `JOIN` с другими операторами через эту связь
(`filter(tags=1, tags__name="y")` требует одного тега, подходящего под оба условия), отдельный вызов
`.filter()` получает свой `JOIN` (`filter(tags=1).filter(tags=2)` находит строку, связанную с обоими, — и
дальше по пути, `Review.objects.filter(book__tags=1).filter(book__tags=2)`), а проверки размножения строк
агрегатами видят его как любой другой `JOIN` — `filter(tags__in=[...]).annotate(n=Count("tags"))` считает
только подходящие теги. Оператор «многие-ко-многим» сравнивает колонку связи в промежуточной таблице,
поэтому сама связанная таблица не присоединяется.

## <a id="filtered-relation"></a>`FilteredRelation`

`FilteredRelation(relation_name, *, condition=Q(...))` (`hare.query.expressions`) присоединяет связь под
собственным именем с условием в `ON` этого `JOIN`: по этому имени видны только связанные строки,
подходящие под условие, а строка без таких всё равно приходит, со значениями `NULL`, — это `LEFT JOIN`:

```python
from hare.query.expressions import FilteredRelation, Q

in_print = FilteredRelation("books", condition=Q(books__in_print=True))
await Author.objects.alias(in_print=in_print).annotate(
    in_print_count=Count("in_print")
).values("name", "in_print_count")
await Author.objects.alias(in_print=in_print).filter(in_print__title__icontains="dune")
await Author.objects.alias(in_print=in_print).values("name", "in_print__title").order_by("in_print__title")
```

- Имя задаётся через `alias()`; `annotate()` делает то же самое: это `JOIN`, а не выбираемое значение.
- С имени начинаются пути, как со связи, — в фильтрах (`in_print__title`, `in_print__isnull=False`, `Q`,
  `exclude()`), `F()`, агрегатах (`Count("in_print")`), `values()` и `order_by()`; путь может идти дальше
  по связям связанной модели (`in_print__tags__name`). Функция, которой путь передан строкой, читает его
  так же (`Upper("in_print__title")`, `Max(Length("in_print__title"))`). Само имя читает первичный ключ
  связанной строки; у составного нет одной колонки, поэтому чтение или сравнение самого имени даёт
  `QueryError` с именами составляющих — `__isnull` работает.
- `relation_name` — связь модели или путь из связей (`"books__reviews"`, условие — на последней); прямая,
  обратная или «многие-ко-многим». У «многие-ко-многим» условие убирает из `JOIN` и связи со строками,
  которые оно не пропускает, поэтому такая связь не добавляет строки.
- Собственное имя связи сохраняет свой `JOIN` — `filter(books__title="x")` рядом с отфильтрованным именем
  присоединяет таблицу дважды; проверки размножения строк агрегатами считают отфильтрованное имя
  отдельным `JOIN` «ко многим» (`Count("in_print")` рядом с `Count("books")` требует `distinct=True`).
- `condition` читает поля самой связи, записанные через её имя (`Q(books__in_print=True)`,
  `Q(books__price__lt=F("books__list_price"))`); ключ или `F()`, которые не начинаются с него, дают
  `QueryError`, как и условие через следующую связь или `Exists(...)`. `relation_name`, который не
  является связью, даёт `FieldError`, и это проверяется первым. Обе проверки выполняются, когда
  `alias()`/`annotate()` даёт `FilteredRelation` имя, даже если запрос его не читает.
- Запрос с отфильтрованной связью не хранит [план](query-plan-cache.ru.md): её условие — часть `JOIN`.

## <a id="lateral"></a>`Lateral`

`Lateral(queryset)` (`hare.query.expressions`) присоединяет запрос `values()`/`values_list()` как
подзапрос `LATERAL` под собственным именем: подзапрос выполняется для каждой строки запроса, читает эту
строку через `OuterReference()` и даёт ей сразу несколько колонок — там, где вычисляемое значение
`Subquery` даёт одну. Строка, для которой подзапрос ничего не вернул, всё равно приходит, её колонки —
`NULL`: это `LEFT JOIN LATERAL ... ON TRUE`:

```python
from hare.query.expressions import Lateral, OuterReference

best = Lateral(
    Book.objects.filter(author=OuterReference("pk")).order_by("-rating", "id").values("name", "rating")[:1]
)
await Author.objects.alias(best=best).values("name", "best__name", "best__rating")
# [{"name": "Tolkien", "best__name": "Rings", "best__rating": 5.0},
#  {"name": "Nobody", "best__name": None, "best__rating": None}, ...]
await Author.objects.alias(best=best).filter(best__rating__gte=4.5).order_by("-best__rating")
authors = await Author.objects.alias(best=best).annotate(best_name=F("best__name"), loud=Upper("best__name"))
```

Запрос, который он строит:

```sql
SELECT "author"."name", "best"."name", "best"."rating" FROM "author"
LEFT OUTER JOIN LATERAL (
  SELECT "name", "rating" FROM "book" WHERE "author_id" = "author"."id"
  ORDER BY "rating" DESC, "id" ASC LIMIT 1
) "best" ON TRUE
```

- Имя задаётся через `alias()`; `annotate()` делает то же самое: это `JOIN`, а не выбираемое значение.
  Чтобы колонка попала в объекты модели, её нужно вычислить (`annotate(best_name=F("best__name"))`).
- `<name>__<column>` читает колонку, которую выбирает запрос, — имена его `values()` или поля
  `values_list()` — в фильтрах (`best__rating__gte=4.5`, `best__name__isnull=True`, `Q`, `exclude()`),
  `F()`, функциях (`Upper("best__name")`), агрегатах (`Sum("best__rating")`), `values()`/`values_list()`
  и `order_by()`. Колонка, которую запрос не выбирает, даёт `FieldError`, как и само имя в
  `values()`/`values_list()`.
- Колонки сохраняют типы: значения поля модели раскодируются как у этого поля, вычисляемого значения —
  как у его поля результата (`Count(...)` — целое число).
- Строка приходит по одному разу на каждую строку подзапроса — срез `[:1]` оставляет одну; `[:3]` даёт
  три лучшие книги каждого автора тремя строками, как `JOIN` связи «ко многим». С агрегатом внутри
  (`values("author").annotate(books=Count("id")).values("books")`) подзапрос возвращает одну строку.
  `Lateral` без среза или больше чем из одной строки повторяет строки, даже если его имя нигде не читается.
- Несколько `Lateral`, а также `Lateral` рядом с `FilteredRelation`, присоединяются рядом друг с другом.
- `queryset` должен быть запросом `values()`/`values_list()`; другое значение даёт `QueryError` при
  создании `Lateral`.
- Только PostgreSQL (`features.supports_lateral`): на другой базе запрос, читающий имя, даёт
  `UnSupportedError` до отправки SQL. Запрос с `Lateral` не хранит [план](query-plan-cache.ru.md): его
  подзапрос — часть `JOIN`.

## <a id="asof-join"></a>`AsofJoin`

`AsofJoin(source, on=Q(...))` (`hare.query.expressions`) присоединяет строки модели способом `ASOF` под
собственным именем: каждая строка запроса получает одну строку `source`, ближайшую к ней по неравенству
среди строк, равных ей по остальным условиям, — последнюю котировку инструмента на момент каждой сделки
или раньше. Строка, для которой такой нет, всё равно приходит, её значения — `NULL`: это
`ASOF LEFT JOIN`. `source` — модель (класс или метка `"app.Model"`) или queryset её строк:

```python
from hare.query.expressions import AsofJoin, OuterReference, Q

quote = AsofJoin(Quote, on=Q(symbol=OuterReference("symbol"), quoted_at__lte=OuterReference("traded_at")))
await Trade.objects.alias(quote=quote).values("id", "quote__price")
# [{"id": 1, "quote__price": Decimal("1.00")}, {"id": 3, "quote__price": None}, ...]
await Trade.objects.alias(quote=quote).filter(quote__price__gt=1).order_by("traded_at")
await Trade.objects.alias(quote=AsofJoin(Quote.objects.filter(source="exchange"), on=...)).values("quote__price")
```

Запрос, который он строит:

```sql
SELECT "trade"."id", "quote"."price" FROM "trade"
ASOF LEFT JOIN "quote" "quote" ON "quote"."symbol" = "trade"."symbol" AND "quote"."quoted_at" <= "trade"."traded_at"
```

- `on` сравнивает поля присоединяемой модели с полями строки запроса, каждое прочитанное через
  `OuterReference()`: хотя бы одно равенство (`symbol=OuterReference("symbol")`) и ровно одно неравенство
  — `__lt`, `__lte`, `__gt` или `__gte` — по полю, по которому ищется ближайшая строка. Всё остальное —
  простое значение, другой оператор фильтра, `|`, `~`, вложенный `Q`, поле связи — даёт `QueryError`.
- `source` — присоединяемая модель или запрос её строк (`Quote.objects.filter(...)`), присоединяемый
  подзапросом; запрос `values()` даёт `QueryError`.
- Имя задаётся через `alias()`; `annotate()` делает то же самое: это `JOIN`, а не выбираемое значение.
  `<name>__<field>` читает поле присоединённой строки в фильтрах, `F()`, функциях, агрегатах,
  `values()`/`values_list()` и `order_by()`; само имя читает её первичный ключ (`quote__isnull=True`
  находит строки без ближайшей строки) — составной только через `__isnull`, всё остальное даёт
  `QueryError` с именами его составляющих.
- К каждой строке присоединяется не больше одной строки: строки не повторяются, и `count()` считает
  строки запроса.
- Только на базе с `features.supports_asof_join` — ClickHouse: на другой базе запрос, читающий имя, даёт
  `UnSupportedError` до отправки SQL. Запрос с `AsofJoin` хранит [план](query-plan-cache.ru.md).

## <a id="array-join"></a>`ArrayJoin`

```python
class ArrayJoin:
    def __init__(self, array: str | F | Expression, *, left: bool = False) -> None: ...
```

`ArrayJoin(array)` (`hare.query.expressions`) повторяет каждую строку с каждым элементом массива —
`ARRAY JOIN` — под именем, которое даёт `alias()`/`annotate()`:

```python
from hare.query.expressions import ArrayJoin

tagged = Book.objects.alias(tag=ArrayJoin("tags"))   # tags = fields.ArrayField(fields.CharField(max_length=20))
await tagged.filter(tag__startswith="fan").values_list("id", "tag")
# [(1, "fantasy"), (4, "fantasy")]
await tagged.count()   # по строке на элемент: 3 книги с 2, 0 и 1 тегом дают 3
```

```sql
SELECT "book"."id","tag" FROM "book" ARRAY JOIN "book"."tags" AS "tag" WHERE toString("tag") LIKE 'fan%'
```

- `<name>` читает элемент в фильтрах (с операторами фильтра поля элемента), выражениях, `values()` и
  `order_by()`; `<name>__<path>` читает внутри него, как путь `base_field` массива (`item__sku` у массива
  именованных кортежей).
- Строки повторяются, читает ли что-нибудь имя или нет: `count()`, `exists()`, строки `values()` и
  агрегат читают повторённые строки, как JOIN связи с несколькими строками.
- Строка с пустым массивом не приходит ни с одним элементом; с `left=True` она приходит один раз, её
  элемент — значение по умолчанию типа элемента (`""`, `0`).
- `array` — имя поля, `F()` или выражение `ArrayField`; всё остальное поднимает `QueryError`.
- Только на базе с `features.supports_array_join` — ClickHouse: на другой запрос с ним поднимает
  `UnSupportedError` до отправки SQL.

## <a id="json-table"></a>`JsonTable`

```python
class JsonTable:
    def __init__(
        self,
        document: str | Expression,
        path: str,
        columns: Mapping[str, Field[Any] | tuple[Field[Any], str]],
        *,
        ordinality: str | None = None,
    ) -> None: ...
```

Элементы JSON-документа как строки, присоединённые под именем, которое даёт `alias()`/`annotate()`, —
издания из сведений о книге, теги профиля, — читаются, фильтруются и сортируются как колонки
(`JSON_TABLE`):

```python
from hare.query.expressions import JsonTable

editions = JsonTable(
    "details",                     # JSONField — имя, F() или выражение
    "$.editions[*]",                   # JSON-путь элементов
    {
        "isbn": fields.CharField(max_length=13),            # читается по $.isbn каждого элемента
        "pages": fields.IntField(),
        "publisher": (fields.TextField(), "$.publisher.name"),  # другой путь
    },
    ordinality="position",          # нумерует элементы с 1
)
await Book.objects.alias(edition=editions).filter(edition__pages__gte=300).values("id", "edition__isbn", "edition__position")
```

```sql
SELECT "book"."id","edition"."isbn","edition"."position" FROM "book"
LEFT OUTER JOIN JSON_TABLE("book"."details", '$.editions[*]' COLUMNS ("position" FOR ORDINALITY,
  "isbn" VARCHAR(13) PATH '$.isbn', "pages" INT PATH '$.pages', "publisher" TEXT PATH '$.publisher.name')) "edition"
  ON TRUE
WHERE "edition"."pages">=$1
```

- `<name>__<column>` читает колонку в фильтрах (с операторами колонки), выражениях, `values()` и
  `order_by()`; каждая колонка читается так, как значение читает её поле, — значение, не приводимое к
  типу колонки, даёт NULL. Само имя ничего не выбирает.
- Строка приходит по одному разу на каждый элемент. Строка, в документе которой там нет элементов, —
  пустой массив, нет ключа, документ NULL, — всё равно приходит один раз, её колонки NULL. Строки
  повторяются, читает ли что-нибудь колонку или нет: их читают `count()`, `exists()`, строки `values()` и
  агрегат.
- Колонка — поле, читаемое по `$.<name>` элемента, или пара `(field, path)`. Путь, не начинающийся с `$`,
  имя колонки, которое не идентификатор без `__`, ordinality с именем колонки, колонка, не являющаяся
  полем, дают `QueryError` при создании `JsonTable`; чтение колонки, которой у него нет, — `FieldError`.
- PostgreSQL 17+ (`features.supports_json_table`): на другом сервере запрос, читающий имя, даёт
  `UnSupportedError` до отправки SQL.

## <a id="jsonfield"></a>`JSONField`

Полностью отдельный набор (общий набор выше **не** применяется): `exact`, `not`, `in`, `not_in`,
`isnull`, `not_isnull`, `gt`, `gte`, `lt`, `lte`, `range` (в порядке самой базы — см.
[`F("data__key")`](expressions.ru.md)), `contains`, `contained_by`, `has_key`, `has_keys`,
`has_any_keys`, `filter` (оператор по вложенному пути в словаре).

Любое другое имя после поля — это ключ: `filter(data__owner__name="a")`, `filter(data__score__gt=10)`,
`filter(data__tags__0="x")`, `exclude(data__owner__isnull=True)` читают значение JSON по этому пути и
принимают его операторы — так же, как фильтр по [`F("data__owner__name")`](expressions.ru.md). Их
`gt`/`gte`/`lt`/`lte`/`range` сравнивают в собственном порядке базы значение JSON по пути. PostgreSQL
использует порядок `jsonb`: значение другого типа JSON сравнивается по типу (`null` < строка < число <
логическое < массив < объект), поэтому `data__score__gt=10` находит `true`. SQLite сравнивает значение,
которое читает `json_extract()`: строку — как саму себя, `true`/`false` — как 1/0, JSON `null` — как `NULL`,
объект или массив — как его сжатый текст; порядок — сначала числа, потом текст. ClickHouse сравнивает
значение `Dynamic`, которое читает из текста JSON (настройка сессии `allow_suspicious_types_in_order_by=1`):
`Dynamic` упорядочивается сначала по имени своего типа, потом по значению (`Float64` `2.5` раньше `Int64`
`9`, `Bool` перед числами, строки после), а сравнение пути, где лежат значения без общего типа, —
строки и числа, объекты и числа, — даёт собственную ошибку ClickHouse (`NO_COMMON_TYPE`,
`ILLEGAL_TYPE_OF_ARGUMENT`). `date`/`datetime` — это текст ISO-8601, который сравнивается как строка:
`data__at__gt=date(...)` находит любой более поздний текст. `__filter` вместо этого сравнивает по типу
значения (ниже). Путь не может назвать пустой ключ (`data__`, `data____isnull` дают `FieldError`, как любой
ключ с пустой частью) — такой ключ находят через `data__contains={'': value}` (PostgreSQL) или
`data__has_key=''` (SQLite до 3.41 сама отклоняет путь `$.""`). Имя
оператора сразу после поля (`data__contains`) — это оператор для всего значения; ключ, названный как
оператор, доступен через `__filter` (`data__filter={"contains": 1}`). Те же пути работают внутри
вычисляемого значения JSON (`annotate(summary=JSONObject(...)).filter(summary__total__gte=10)`) и после
связи (`filter(owner__data__plan="pro")`). В SQLite они читают значение через `json_extract()` и несколько
встроенных функций SQL, которые hare регистрирует в каждом соединении. Текстовый оператор фильтра пути
(`data__code__contains="1"`) сопоставляется с собственным текстом значения в базе: в PostgreSQL это то, что
даёт `->>` (строка — сама собой, `true`/`false`, число — точными цифрами, `1e20` — как
`100000000000000000000`, объект или массив — как его пишет `jsonb`), в SQLite — то, что даёт
`json_extract()`; ClickHouse сопоставляет число с экспонентой так, как оно записано. `__filter={...}`
поддерживает вложенные ключи-операторы: `not`, `isnull`, `not_isnull`, `has_key`, `has_keys`,
`has_any_keys`, `in`, `not_in`, `gte`, `gt`, `lte`, `lt`, `range`, `contains`, `startswith`, `endswith`,
`iexact`, `icontains`, `istartswith`, `iendswith`, `year`, `quarter`, `month`, `week`, `day`, `hour`,
`minute`, `second`.

Тип сравнения определяется значением: число сравнивается как число, строка — как текст
(`{"name__gt": "b"}`), `bool` — как логическое значение, `date`/`datetime` — как момент времени.
`datetime` с поясом сравнивается как `timestamptz` с учётом собственного смещения сохранённой строки;
`datetime` без пояса сравнивает цифры времени на часах, не учитывая сохранённое смещение. Сохранённое
значение другого типа JSON никогда не совпадает — в том числе строка при сравнении дат, если это не дата
или дата-время ISO-8601 (или она называет день, которого нет в этом месяце): такая строка просто не
подходит. Значение `dict`/`list` сравнивается со значением `jsonb` по пути (`{"a": {"x": 1}}`,
`{"a__in": [[1, 2]]}`); принимают его только `exact`/`not`/`in`/`not_in`.
`contains`/`startswith`/`icontains`/... принимают строку (другое значение даёт `QueryError`) и совпадают
только с сохранённой строкой. Оператор можно записать и вложенным словарём с одним ключом —
`{"enabled": {"not": False}}` то же самое, что `{"enabled__not": False}` (словарь с одним ключом, который
является именем оператора, всегда читается как оператор; чтобы найти такой объект буквально, используйте
`__contains`). Значения одного списка `in`/`not_in` должны быть одного типа, границы `range` — отдельными
значениями, а ключ — текстом, иначе `QueryError`, — кроме
`None`, который совпадает с JSON `null` или отсутствующим ключом (`{"a__in": [1, None]}` совпадает с
числом `1`, с `null` и с отсутствием `a`; `not_in` их отбрасывает). Часть пути из цифр — это ключ объекта
или индекс массива; как индекс она совпадает только внутри массива, никогда со скалярным значением.
Отрицательный индекс отсчитывается с конца массива (`{"scores__-1": 30}`, также `F("data__scores__-1")`).

`exact`/`not`/`in`/`not_in` по всей колонке сравнивают так, как сравнивает база. PostgreSQL сравнивает
значения `jsonb`, поэтому порядок ключей, пробелы и разница между `1` и `1.0` не важны. SQLite сравнивает
хранимый текст, поэтому порядок ключей и разница между `1` и `1.0` важны, а контейнер в `__filter`
сравнивается как сжатый текст JSON. В колонке ClickHouse `JSON` значение приводится к `JSON` и сравнивается
самим ClickHouse (порядок ключей не важен; `__in` сравнивает так каждое значение, через `OR`; значение,
которое не объект, даёт `ValidationError`: колонка хранит только объект); колонка `String` с текстом JSON
сравнивается как текст. `None` в списке `in`/`not_in` означает колонку SQL `NULL`. `contains`/`contained_by`
— это `@>`/`<@` у `jsonb` PostgreSQL: объект содержит другой, если в нём есть каждый ключ другого с
содержащимся значением; массив содержит другой, если каждый элемент другого содержится в каком-то его
элементе (порядок и повторы не важны); и только массив верхнего уровня содержит также отдельное скалярное
значение (`data__contains="bar"` совпадает с `["foo", "bar"]`). В JSON SQLite и ClickHouse вхождения нет:
там они дают `UnSupportedError` до отправки SQL.

```python
await Book.objects.filter(details__contains={"formats": ["hardback"]})
await Book.objects.filter(details__filter={"illustrated": {"not": False}})
```

### <a id="key-existence"></a>Наличие ключа: `has_key` / `has_keys` / `has_any_keys`

`?` / `?&` / `?|` из `jsonb` в PostgreSQL, `json_type()` в SQLite, `JSONHas()` в ClickHouse. Они проверяют
ключи **верхнего уровня** сохранённого объекта (вложенный ключ не считается):

| Оператор | Значение | Подходят строки, где |
|---|---|---|
| `data__has_key="isbn13"` | строка | ключ есть |
| `data__has_keys=["a", "b"]` | список строк | есть **все** ключи (пустой список подходит к каждой строке, где есть документ) |
| `data__has_any_keys=["a", "b"]` | список строк | есть **хотя бы один** ключ (пустой список не подходит ни к чему) |

Внутри `__filter` эти три работают как ключи-операторы на вложенном пути —
`data__filter={"publisher__has_key": "city"}` проверяет объект в `data->'publisher'`. Без пути перед
ним имя оператора — это ключ: `data__filter={"has_key": "publisher"}` сравнивает ключ `has_key` с
`"publisher"`, а саму колонку проверяет `data__has_key="publisher"`. Ключ со значением JSON `null` считается
существующим. Колонка SQL `NULL` (документа нет вовсе) не подходит ни под один из трёх. `?` в PostgreSQL
также совпадает со строковым элементом массива верхнего уровня или со строкой верхнего уровня, равной
ключу, а SQLite и ClickHouse находят только ключ объекта.

```python
await Book.objects.filter(details__has_key="isbn13")
await Book.objects.filter(details__has_keys=["isbn13", "pages"])
await Book.objects.filter(details__has_any_keys=["isbn13", "doi"])
await Book.objects.filter(details__filter={"publisher__has_key": "city"})
```

> [!WARNING]
> **`__isnull` в `__filter` означает, что значения там нет: и JSON `null`, и отсутствующий ключ**
>
> `__filter={"key__isnull": True}` (и `{"key": None}`) построен на извлечении текста через `->>`, которое
> возвращает SQL `NULL` в **трёх** случаях: ключ есть со значением JSON `null`, ключа нет в объекте, вся
> колонка JSON равна SQL `NULL`. `isnull=True` подходит ко всем трём; `isnull=False` — только к
> существующему значению, не равному `null`. Так устроен `->>`, это не ошибка. Чтобы различить случаи,
> сочетайте `isnull` с `has_key`:
>
> ```python
> # ключ есть, значение — JSON null
> Q(data__filter={"key__isnull": True}) & Q(data__has_key="key")
> # ключа нет в объекте (или документа нет вовсе)
> Q(data__filter={"key__isnull": True}) & ~Q(data__has_key="key")
> # ключ есть с настоящим (не null) значением
> Q(data__filter={"key__isnull": False})
> ```
>
> Собственные операторы пути различают эти случаи на любой базе: `data__key__isnull=True` подходит к
> отсутствующему ключу (и к строке без документа), `data__key=None` — к JSON `null`.

## <a id="custom-lookups"></a>Свои операторы фильтра

Зарегистрируйте свой через [`Field.register_lookup()`](../extending/custom-lookups.ru.md) — встроенные
окончания выше всегда важнее, свой оператор не может заменить ни одно из них. Все операторы поля вместе с
принимаемым значением и диалектами, на которых они работают, перечисляет
[`get_lookups()`](describing-filters.ru.md#get_lookups).

`Q`, `F`, `Case`/`When`, `Exists`/`OuterReference`/`Subquery`, `RawSQL`, оконные функции и
`hare.query.functions` (`Count`/`Sum`/`Max`/`Min`/`Avg`/строковые функции/свои функции) описаны на
отдельной странице — см. [Выражения](expressions.ru.md).

## <a id="null-semantics"></a>Как работает NULL

`exclude()`/`~Q(...)` оставляют строку в результате, **если только исключаемое условие не равно точно
`TRUE`** — и `FALSE`, и `NULL`/`UNKNOWN` (трёхзначная логика SQL) считаются «не подошло». Для колонки,
допускающей `NULL`:

```python
await Book.objects.exclude(subtitle="Book One")   # оставляет строку, где subtitle IS NULL — NULL не равен "Book One"
await Book.objects.exclude(rating__gt=1)            # оставляет строку, где rating IS NULL — NULL не больше 1
await Book.objects.exclude(editor=some_reader)       # оставляет строку, где editor IS NULL (редактора нет)
await Book.objects.exclude(editor__name="Ann")       # так же — через связь
```

Это одинаково относится к колонке самой модели и к связи, через которую идёт условие. Для составного
условия (`exclude(Q(a) | Q(b))`) это действует для условия целиком, а не для каждой части: строка
остаётся, если только ВСЁ составное условие не равно `TRUE`.

Колонку без `NULL` это не затрагивает — у неё нет строки `NULL`, которую надо было бы оставить, и SQL не
меняется.

Агрегатное вычисляемое значение, отфильтрованное в `HAVING`, следует тому же правилу:
`annotate(total=Sum("books__pages")).exclude(total__gt=500)` оставляет автора без книг, у которого `SUM`
равен `NULL`.

Тот же принцип «обращаться с `NULL` так, как это сделали бы `not in`/`!=` в Python» действует ещё для
двух операторов:

- `field__not_in=<queryset/Subquery>` / `exclude(field__in=<queryset/Subquery>)`: строка `NULL` в
  результате подзапроса не опустошает весь результат (как это сделал бы `NOT IN (..., NULL)` в SQL,
  который всегда `UNKNOWN`) — значение, которого действительно нет среди значений подзапроса, не равных
  `NULL`, остаётся, как `x not in [1, None]` в обычном Python. `field__in`/`.filter()` со списком Python
  так же обрабатывают `None` внутри списка. Значение NULL подходит под `__in`, только если подзапрос
  содержит NULL; `__not_in`, `exclude(__in=...)` и `~Q(...__in=...)` тогда его исключают.
  Набор объектов внутри `Subquery(...)` сужается до первичного ключа так же, как набор без обёртки.
- `field__not=F(other_field)` (или любое другое выражение справа): сравнивается через `IS DISTINCT FROM`
  в SQL (`IS NOT` в SQLite, с тем же смыслом), а не через простое `<>`, — одинаково правильно в обе
  стороны при `NULL`: строка остаётся, если стороны действительно различаются, в том числе когда `NULL`
  только одна из них, и исключается, когда обе `NULL` (два `NULL` не считаются различными). Простое
  значение справа (`field__not="foo"`) этого не затрагивает — только выражение.

## <a id="field-names-from-user-input"></a>Имена полей из пользовательского ввода

Значения, переданные в `filter()`/`exclude()`, всегда передаются как параметры, а вот **имена** полей — в
`order_by()`, `values()`/`values_list()`, ключах `filter()`/`exclude()`,
`select_related()`/`prefetch_related()`, `only()` — считаются доверенными: hare разбирает любой
переданный путь через связи и не ограничивает его длину. Каждый шаг добавляет `JOIN` (шаг
«многие-ко-многим» — два), а путь через несколько связей «ко многим» умножает число строк, которые должна
построить база. Поэтому имя, взятое прямо из запроса (`?sort=company__docs__company__docs__...`),
позволяет клиенту заставить базу выполнить сколько угодно работы — и прочитать поля, которые вы не
собирались показывать (`?sort=owner__password_hash` раскрывает его через порядок строк). Встроенного
ограничения глубины нет; сопоставляйте ввод пользователя с именами полей через белый список:

```python
SORTABLE_FIELDS = {"title": "title", "-title": "-title", "published": "published_at", "-published": "-published_at"}

async def list_books(sort: str) -> list[Book]:
    ordering = SORTABLE_FIELDS.get(sort)
    if ordering is None:
        raise ValueError(f"unsupported sort key {sort!r}")
    return await Book.objects.all().order_by(ordering)
```

Для каждого ключа из белого списка [`get_lookup_info()`](describing-filters.ru.md) сообщает, через какие
связи он проходит и значение какого типа принимает, поэтому параметр запроса можно разобрать до того, как
он попадёт в `.filter()`.

В SQLite запрос, соединяющий больше 64 таблиц, даёт `OperationalError` с этим объяснением.

Значение, которое тип целого поля не вмещает (`small__gt=100000` у `SmallIntField`, `tiny__in=[256, 1]` у
`UInt8Field` ClickHouse), даёт `ValidationError` до отправки команды на любой базе, как и при записи:
драйвер связывает параметр как тип колонки и не смог бы его отправить. Для такого значения сравнивайте
более широкое выражение (`Cast("small", BigIntField())`).

Имя, которое записывается в текст SQL, — выходное имя `values()`/`annotate()`, ключ JSON в пути
(`data__key`, `F("data__key")`, `data__filter={...}`) — не может содержать нулевой байт (`"\x00"`): это
даёт `ValidationError` до отправки команды, на любой базе. В PostgreSQL готовый текст SQL с таким байтом
(`RawSQL(...)`, `execute(...)`) тоже даёт `ValidationError`, а не ошибку драйвера, похожую на потерянное
соединение; передавайте такое значение параметром. В SQLite образец оператора семейства `LIKE`
(`contains`, `startswith`, `iexact`, ...) с нулевым байтом тоже даёт `ValidationError`, потому что SQLite
читает образец только до него; любое другое значение с нулевым байтом хранится так, как его хранит база.
