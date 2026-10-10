# Проверка типов запросов

Плагин mypy `hare.contrib.mypy` сверяет запросы с моделями проекта, пока mypy проверяет код. Он
находит ключ фильтра, которого у модели нет, оператор, которого нет у поля, значение не того типа,
опечатку в имени в `order_by()` или `only()`, поле, которого не знает конструктор. Без плагина всё это
обнаружилось бы только при выполнении запроса. Кроме того, плагин даёт строкам `values()` и
`values_list()` настоящие типы вместо `dict[str, Any]` и `tuple[Any, ...]`.

Плагин читает модели так же, как hare во время работы, — через `Model._meta.get_lookup_info()`
([Описание фильтров и сортировок](describing-filters.ru.md)). Поэтому он принимает ровно то, что
принимает запрос, его тексты ошибок совпадают с теми, что бросил бы запрос, а операторы,
преобразования и поля, которые проект регистрирует сам, проверяются так же, как встроенные.

## <a id="setup"></a>Подключение

```bash
pip install "hare-orm[mypy]"
```

Плагин включается в конфигурации mypy. Ему нужно знать, где конфигурация hare, — это та же настройка
`[tool.hare] hare_orm`, которую читает команда `hare` (или переменная окружения `HARE_ORM`):

```toml
# pyproject.toml
[tool.mypy]
plugins = ["hare.contrib.mypy"]

[tool.hare]
hare_orm = "myproject.settings.HARE_CONFIG"
```

В начале каждого запуска mypy плагин импортирует конфигурацию и связывает её модели без базы данных
(`Hare.bind_models()`): связи, заменяемые модели, операторы фильтров. Для импорта в путь Python
добавляется папка файла конфигурации mypy. Операторы каждой модели сверяются с диалектом её
`default_connection`.

### <a id="mypy-imports"></a>Операторы, зарегистрированные вне моделей

Оператор, преобразование или класс поля, зарегистрированные при импорте модулей моделей, плагин видит
сразу. Если регистрация происходит в другом месте — например, в коде запуска приложения, — этот модуль
нужно назвать, и плагин импортирует его до связывания моделей:

```toml
[tool.hare]
hare_orm = "myproject.settings.HARE_CONFIG"
mypy_imports = ["myproject.lookups"]
```

`mypy_imports` — список имён модулей.

### <a id="editor"></a>В редакторе

Плагин работает везде, где работает mypy: в командной строке, в `dmypy` и в редакторах, которые
запускают mypy. В VS Code это расширение
[Mypy Type Checker](https://marketplace.visualstudio.com/items?itemName=ms-python.mypy-type-checker),
оно читает тот же `pyproject.toml`.

## <a id="checked"></a>Что проверяется

У каждой ошибки плагина код `hare-query`, поэтому одну строку можно заглушить через
`# type: ignore[hare-query]`.

### <a id="filters"></a>Фильтры

Проверяются именованные аргументы `filter()`, `exclude()`, `get()`, `get_or_create()` и
`update_or_create()` — у `Model.objects`, у менеджера связи и у QuerySet собственного класса:

```python
Book.objects.filter(title__icontains="war", author__born__year__gte=1800)

Book.objects.filter(titel="War")
# error: Unknown filter param 'titel': Book has no field 'titel'  [hare-query]
Book.objects.filter(title__nope="War")
# error: Unknown filter param 'title__nope': Book.title has no lookup 'nope'  [hare-query]
Book.objects.filter(published__year="1869")
# error: Argument "published__year" to "filter" of "QuerySet" has incompatible type "str";
#        expected "int | Expression | Term | QuerySpecification[Any]"  [arg-type]
```

Какое значение принимает ключ:

| Ключ | Значение |
|---|---|
| поле (`title`) и оператор, сравнивающий его значение (`title__gte`, `title__icontains`) | тип поля, как его объявляет модель; поле перечисления (`CharEnumField(Status)`) принимает член перечисления или его значение (`"open"`) |
| типизированное поле JSON (`JSONField[Address]`) | объявленный тип для равенства (`address={...}`); остальные операторы (`address__contains`) принимают любую часть документа |
| оператор списка (`title__in`) | итерируемое значений этого типа |
| `__range` | кортеж или список из двух значений; граница None оставляет эту сторону открытой |
| `__isnull` | `bool` |
| часть даты (`published__year`), `__len` | `int` |
| `published__date`, `published__time` | `date`, `time` |
| прямая связь (`author`, `author__in`) | её ключ или экземпляр связанной модели |
| связь на составной ключ, `pk` составного ключа | кортеж типов ключа или экземпляр |
| обобщённая связь (`subject`) | экземпляр одной из её моделей |
| `subject__type` | одно из имён веток — `Literal["post", "photo"]` |
| путь в JSON (`data__owner__name`) | что угодно |
| зарегистрированный оператор | по его `value_shape` и `value_type` (при None — тип поля) |
| зарегистрированное преобразование (`rating__as_text__startswith`) | тип поля, которое читает преобразование |
| аннотация | её тип (см. ниже) |

Равенство (`title=None`) принимает и None, если значение может быть NULL: поле допускает NULL или
ключ проходит через связь, допускающую NULL, или через связь со многими строками. Любой ключ принимает
также выражение (`F()`, функцию, агрегат), терм или подзапрос (QuerySet).

Сообщается и об операторе, который существует, но не выполняется на базе модели:

```python
Writer.objects.filter(rating__within_on_postgresql=(1, 2))
# error: Filter param 'rating__within_on_postgresql': Writer.rating has no lookup
#        'within_on_postgresql' on the sqlite database  [hare-query]
```

### <a id="values"></a>Строки `values()` и `values_list()`

```python
rows = await Book.objects.values("id", "title", "author__name", "shelf__label")
reveal_type(rows[0])
# TypedDict({'id': int, 'title': str, 'author__name': str, 'shelf__label': str | None})

pairs = await Book.objects.values_list("id", "title")       # list[tuple[int, str]]
titles = await Book.objects.values_list("title", flat=True)  # list[str]
first = await Book.objects.values_list("title", flat=True).first()  # str | None
```

- `values()` без аргументов — все хранимые поля модели (связь — по её колонке, `author_id`) и все
  аннотации, кроме `.alias()`.
- `values(name="title")` выбирает путь под этим именем, `values(total=Sum("price"))` — выражение с
  типом аннотации.
- Значение, прошедшее через связь, допускающую NULL, или через связь со многими строками, может быть
  None.
- Имя, которое не является путём поля или является оператором (`title__icontains`), — ошибка.
- `annotate()` после `values()` добавляет свои имена в строку.
- `get()`, `first()`, `last()` и перебор дают тип строки.
- Строка `values_list(named=True)` остаётся `Any`: её класс создаётся во время работы.

### <a id="annotations"></a>Аннотации

`annotate()` и `alias()` добавляют свои имена в тип QuerySet — дальше их можно указывать в ключах
фильтров, сортировках и именах `values()`:

```python
books = Book.objects.annotate(chapter_count=Count("chapters"), total=Sum("price"))
books.filter(chapter_count__gte=10).order_by("-total")
```

| Выражение | Тип |
|---|---|
| `Count`, `Length` | `int` |
| `Exists` | `bool` |
| `Sum`, `Min`, `Max` | тип поля или None (строк нет) |
| `F("path")` | тип пути |
| `Value(x)` | тип `x` |
| остальное | `Any` |

Имена и типы хранятся в третьем параметре типа QuerySet в виде `TypedDict` —
`QuerySet[Book, Book, TypedDict({'chapter_count': int, ...})]`. Параметр ковариантный, поэтому
QuerySet с аннотациями остаётся `QuerySet[Book]`. Собственный класс QuerySet, который не передаёт этот
параметр дальше (`class BookQuerySet(QuerySet[Book])`), хранит аннотации скрыто от глаз — вместе со
своими методами.

Функция, принимающая QuerySet с аннотациями, называет их в третьем параметре или ставит там `Any` —
тогда любое имя считается возможной аннотацией:

```python
def popular(books: QuerySet[Book, Book, Any]) -> QuerySet[Book, Book, Any]:
    return books.filter(chapter_count__gte=10)
```

### <a id="names"></a>Имена

Проверяются имена, переданные строковыми литералами:

| Метод | Имя — это |
|---|---|
| `order_by()` | путь поля или аннотация, можно с `-`; `?` — случайный порядок |
| `only()` | путь поля или аннотация |
| `defer()` | прямое поле модели, не связь |
| `select_related()` | путь из прямых связей и обратных «один к одному» или обобщённая связь |
| `bulk_update(fields=...)` | поле строк модели, не связь со многими строками |

### <a id="writes"></a>Запись

Именованные аргументы конструктора модели, `create()` и `update()` задают поля: по имени поля, по
колонке связи (`author_id`) или через `pk`. Поле перечисления принимает член перечисления или его значение,
связь — экземпляр своей модели (и None, если
допускает NULL), обобщённая связь — экземпляр одной из своих моделей, а `update()` — ещё и выражение:

```python
Book(title="War and Peace", author=tolstoy)
await Book.objects.filter(id=1).update(price=F("price") * 2)

Book(titel="War and Peace")
# error: Book has no field 'titel' to set  [hare-query]
Book(chapters=[chapter])
# error: Book.chapters is a relation holding many rows - it can't be set  [hare-query]
```

### <a id="dialect-methods"></a>Методы диалекта

Собственные методы `QuerySet` диалекта — `final()`, `prewhere()`, `limit_by()` ClickHouse и другие
(см. [Как написать диалект](../extending/writing-a-dialect.ru.md#queryset-methods)) — известны mypy для
диалектов соединений проекта: каждый принимает параметры своего вызова и возвращает queryset. Метод,
который есть только у диалекта другой базы, — неизвестный атрибут:

```python
PageView.objects.final()                # соединение ClickHouse: ошибки нет
PageView.objects.limit_by()             # error: Missing positional argument "limit" in call to "limit_by"
Book.objects.final()                    # соединение SQLite: error: "QuerySet[...]" has no attribute "final"
```

`hare stubs` так же объявляет их у queryset каждой модели; метод, принимающий условие, принимает ключи
фильтров модели.

## <a id="not-checked"></a>Что не проверяется

- `Q(...)` и `F("...")` — при создании они не привязаны к модели. `Q` проверяется при выполнении
  запроса, а `F` в значении фильтра принимается для любого ключа.
- Имена, заданные не литералами: `filter(**conditions)`, `values(*names)`. Queryset, аннотации
  которого получены через `annotate(**expressions)`, принимает любое имя как аннотацию.
- Написанный вручную SQL.
- Обратные связи — атрибуты, которые mypy видит только если модель их объявляет:
  `books: fields.ReverseRelation["Book"]` ([Связи](../models/relations.ru.md)).

## <a id="cache"></a>Когда модели меняются

mypy сохраняет результаты запуска для следующего. Плагин передаёт mypy отпечаток всего, что он читает
из моделей: полей, связей, допустимости NULL, зарегистрированных операторов и преобразований,
диалектов и их методов `QuerySet`. Любое изменение заставляет mypy проверить модули заново.

## <a id="errors"></a>Когда модели не загружаются

Если конфигурацию не удалось найти или импортировать или модуль моделей падает, плагин сообщает об
этом один раз в каждом проверяемом модуле, а дальше запросы типизируются так, как без плагина:

```text
error: hare: the models can't be loaded for type checking - ConfigurationError: Cannot import
       configuration module 'myproject.settings' ...  [hare-query]
```

## <a id="pyright"></a>pyright и Pylance: `hare stubs`

pyright — и Pylance, расширение Python для VS Code на его основе, — не запускает плагины. Вместо
этого он читает заглушки (stubs): `hare stubs` (`pip install hare-orm[pyright]`) пишет по одной на
каждый модуль моделей в `typings/` — каталог, в котором pyright ищет первым:

```bash
hare stubs                       # typings/myapp/models.pyi, ...
hare stubs --relation-depth 3    # ключи фильтров через связи до 3 уровней (от 0 до 5; по умолчанию 2)
hare stubs --check               # код 1, если заглушки нет или она устарела, — для CI
hare stubs --output stubs         # другая папка вместо typings/
```

Заглушка — весь модуль в том виде, в каком его пишет stubgen из mypy (pyright читает заглушку вместо
модуля, поэтому остальные его имена остаются), с типизированными полями каждой модели
(`name: Field[str]`, связь — `author: Field[Author]`) и QuerySet'ом, который принимает её собственные
ключи и значения:

```python
await Book.objects.filter(titel="x")
# error: No parameter named "titel"
await Book.objects.filter(published__year="2020")
# error: Argument of type "Literal['2020']" cannot be assigned to parameter "published__year"
#        of type "int | Expression | Term | QuerySpecification[Unknown]"
await Book.objects.create(title=1)
# error: Argument of type "Literal[1]" cannot be assigned to parameter "title" of type "str"
titles = await Book.objects.values_list("title", flat=True)  # list[str]
```

| Что типизировано | Через что |
|---|---|
| `filter()`, `exclude()`, `get()`, `get_or_create()`, `update_or_create()` | `<Model>Filters`: каждый ключ полей модели и полей моделей, к которым ведут её связи (до `--relation-depth`), с каждым оператором, который выполняет диалект подключения, и типом его значения — тем же, что у плагина mypy. |
| `create()`, `update()` | `<Model>Writes`: каждое поле, у связи — объект или ключ. |
| `values_list(name, flat=True)` | Тип выбранного поля. |
| Поля модели | `Field[<значение>]` — поле у класса, его значение у экземпляра. |

После изменения моделей `hare stubs` нужно запустить снова; `--check` в CI ловит забытую заглушку.
Плагин mypy проверяет больше, чем может выразить заглушка: строки `values()` и `values_list()` с
несколькими именами, аннотации, имена в `order_by()`/`only()`, выражения в записи. Поле, тип которого
задан в классе модели обобщением (`JSONField[MyDict]`), получает в заглушке тип значения своего класса
поля во время выполнения.
