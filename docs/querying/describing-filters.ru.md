# Описание фильтров и сортировок

`Model._meta.get_lookup_info(key)` сообщает, к чему относится ключ `.filter()`: через какие связи он
проходит, с каким полем сравнивает, какой это оператор фильтра и какое значение он принимает, —
ничего не строя и не выполняя. `get_ordering_info(name)` делает то же для имени в `.order_by()`, а
`get_lookups(path, dialect)` перечисляет все операторы поля, которые выполняет диалект.

Это нужно коду, который превращает внешний ввод в фильтры: слою API, который при запуске проверяет
объявленные фильтры и разбирает каждый параметр запроса в нужный тип, построителю форм, панели
администратора. Такому коду не нужна своя копия списков операторов hare — он спрашивает модель.

hare сам пользуется этими описаниями: `.filter()`, `.exclude()` и `.order_by()` проверяют по ним
каждый ключ при вызове, а запрос сверяет каждый оператор с диалектом своего подключения до
построения SQL.

## <a id="get_lookup_info"></a>`get_lookup_info()`

```python
from hare.query.enums import Lookup, LookupValueShape

info = Event._meta.get_lookup_info("tournament__name__icontains")
info.relations      # (Event.tournament,)
info.field          # Tournament.name
info.lookup         # Lookup.ICONTAINS
info.value_shape    # LookupValueShape.VALUE
info.value_type     # str
```

Принимает любой ключ, который принимает `.filter()`:

| Ключ | `relations` | `field` | `lookup` | `value_shape` | `value_type` |
|---|---|---|---|---|---|
| `name`, `name__icontains` | `()` | `name` | `EXACT`, `ICONTAINS` | `VALUE` | `str` |
| `id__in` | `()` | `id` | `IN` | `LIST` | `int` |
| `id__range` | `()` | `id` | `RANGE` | `RANGE` | `int` |
| `reporter_id__isnull` | `()` | `reporter_id` | `ISNULL` | `VALUE` | `bool` |
| `tournament`, `tournament__in` (ключ или объект) | `(tournament,)` | `Tournament.id` | `EXACT`, `IN` | `VALUE`, `LIST` | `int` |
| `tournament_id__gte` | `()` | `tournament_id` | `GTE` | `VALUE` | `int` |
| `tournament__pk`, `tournament__id__in` | `(tournament,)` | `Tournament.id` | `EXACT`, `IN` | `VALUE`, `LIST` | `int` |
| `participants__in` («многие-ко-многим») | `(participants,)` | `Team.id` | `IN` | `LIST` | `int` |
| `events__isnull` (обратный внешний ключ) | `(events,)` | `Event.event_id` (первичный ключ Event) | `ISNULL` | `VALUE` | `bool` |
| `pk` составного первичного ключа | `()` | `(id, version)` | `EXACT` | `VALUE` | `(UUID, int)` |
| `pk__in` составного первичного ключа | `()` | `(id, version)` | `IN` | `LIST` | `(UUID, int)` |
| `document` — внешний ключ на составной ключ | `(document,)` | `(Document.id, Document.version)` | `EXACT` | `VALUE` | `(UUID, int)` |
| `created__year__gte` | `()` | `created` | `GTE` | `VALUE` | `int` |
| `created__date` | `()` | `created` | `EXACT` | `VALUE` | `date` |
| `data__owner__name__icontains` (JSON) | `()` | `data` | `ICONTAINS` | `VALUE` | `str` |
| `data__has_keys` | `()` | `data` | `HAS_KEYS` | `LIST` | `str` |
| `tags__contains` (массив) | `()` | `tags` | `CONTAINS` | `LIST` | тип элемента |
| `tags__len__gt` | `()` | `tags` | `GT` | `VALUE` | `int` |
| `during__overlap` (диапазон) | `()` | `during` | `OVERLAP` | `RANGE` | тип границы |
| генерируемое поле | `()` | `GeneratedField` | | | тип его `output_field` |
| `name__<свой>` | `()` | `name` | имя оператора | как при регистрации | как при регистрации |

Оператор по самой связи (`tournament=`, `tournament__in=`, `tags__isnull=`) сравнивает ключ связанной
модели, поэтому `field` — это поле ключа: поле (поля), на которое ссылается прямая связь, или
первичный ключ связанной модели для обратной связи и «многие-ко-многим», — а `relations` заканчивается
этой связью.

### <a id="lookupinfo"></a>`LookupInfo`

Неизменяемый класс данных. Описания строятся один раз и сохраняются:

- собственные ключи фильтров модели, имена сортировки (`OrderingInfo`) и результаты `get_lookups()` —
  для каждой модели, а `get_lookups()` — для каждого пути и диалекта;
- ключ или имя, начинающиеся с вычисляемого значения, — для каждого запроса вместе с выходными полями
  вычисляемых значений: копия запроса их делит, а `annotate()`/`alias()`, заменяющие вычисляемое
  значение, их сбрасывают;
- изменение полей, операторов или связей любой модели (регистрация или отмена регистрации модели во
  время работы, добавленное или удалённое поле) сбрасывает все сохранённые описания.

Ключ или имя с ошибкой дают свою `FieldError` заново при каждом вызове — ошибки не сохраняются.

| Атрибут | Что в нём |
|---|---|
| `key` | Ключ фильтра. |
| `model` | Модель, с которой начинается ключ. |
| `relations` | Связи, через которые проходит ключ, по порядку: прямые и обратные внешние ключи и «один-к-одному», поля «многие-ко-многим». |
| `field` | Поле, с которым сравнивается значение; кортеж полей для составного ключа; `None` для вычисляемого значения, тип которого станет известен только при выполнении запроса. |
| `transforms` | Путь, который читается внутри значения поля перед оператором: часть даты (`("year",)`), `date`/`time`, путь по ключам JSON (`("owner", "name")`), индекс массива, срез или `len`, граница или признак диапазона, `unaccent`, ключ hstore. |
| `lookup` | Элемент `Lookup` или имя оператора, зарегистрированного через `register_lookup()`. |
| `value_shape` | `LookupValueShape.VALUE` (одно значение), `LIST` (список: `in`, `not_in`, `has_keys`, `contains` у массива...) или `RANGE` (два элемента: `range`, `overlap` у поля диапазона...). |
| `value_type` | Тип значения — каждого элемента списка или диапазона. Кортеж типов для составного ключа; `bool` для `isnull`/`not_isnull`; `int` для части даты или `len`; `str` для текстового оператора или ключа JSON/hstore; `object` для любого значения JSON. |
| `crosses_to_many` | Проходит ли ключ через связь, у которой на одну строку приходится много строк (обратный внешний ключ или «многие-ко-многим»): фильтр соединяет её, и запрос может вернуть одну строку несколько раз. |
| `requires_extension` | Расширение базы, нужное оператору (`"pg_trgm"` для операторов триграмм, `"unaccent"`), иначе `None`. |
| `dialects` | Имена диалектов, на которых есть поле и оператор (`frozenset({"postgresql"})` у поля массива), `None` — на всех, а также у поля, тип колонки которого даёт каждый диалект (`GeometryField`, `VectorField`): его `is_supported()` проверяет на том диалекте, о котором спрашивают. |
| `is_supported(dialect)` | Выполняет ли запрос на `dialect` этот оператор — см. ниже. |

### <a id="errors"></a>Ошибки

- Путь, который не называет ни поле, ни связь, ни вычисляемое значение, даёт `FieldError`:
  `Unknown filter param 'tournament__nme': Tournament has no field 'nme'`.
- Оператор, которого у поля нет, даёт `FieldError`: `Event.name has no lookup 'foo'`; так же и
  оператор вне `supported_lookups` поля (у зашифрованного поля).
- `pk` составного первичного ключа принимает только `pk=`, `pk__in=`, `pk__not=` и `pk__not_in=`; всё остальное —
  `FieldError`.
- Оператор, отличный от равенства, принадлежности или `isnull`, у прямой связи на составной ключ
  (`document__gt=`) даёт `QueryError`: он был бы неоднозначным — фильтруйте по каждой колонке ключа.
- Модель, которая ещё не привязана (см. [Пока подключения не настроены](#before-the-connections-are-set-up)),
  ничего не описывает: `get_lookup_info()`, `get_lookups()` и `get_ordering_info()` — и у
  `Model._meta`, и у запроса — дают `ConfigurationError`:
  `Book is not bound yet: call Hare.bind_models() or Hare.init() before describing its filters or orderings`.

## <a id="dialect-support"></a>Поддержка диалектом

`dialect.filter_operators.supports_lookup(info)` или `info.is_supported(dialect)` сообщает, выполняет ли запрос на этом
диалекте оператор. Оператор не поддерживается, если:

- поля нет на этом диалекте (`Field.SUPPORTED_DIALECTS`, например поле массива или диапазона вне
  PostgreSQL; поле с `COLUMN_TYPE_FROM_DIALECT`, которому диалект не даёт типа колонки) или свой
  оператор зарегистрирован только для других диалектов;
- оператор реализуют только диалекты (`search`, операторы триграмм, операторы вложенности для
  массивов, диапазонов и JSON), а этот диалект его не реализует;
- оператору нужно расширение, а у диалекта расширений нет.

```python
from hare.dialects.dialect_registry import DialectRegistry

sqlite = DialectRegistry.get_dialect("sqlite")
info = Event._meta.get_lookup_info("name__search")
info.is_supported(sqlite)                                  # False
info.is_supported(DialectRegistry.get_dialect("postgresql"))  # True
```

Запрос с оператором, который диалект его подключения не поддерживает, даёт `UnSupportedError` ещё до
построения SQL: `Event.objects.filter(name__search=...) can't run on sqlite: the sqlite dialect doesn't
implement the __search lookup`.

## <a id="get_lookups"></a>`get_lookups()`

```python
lookups = Event._meta.get_lookups("modified", connection.dialect)
lookups[""]            # простое равенство
lookups["year__gte"]   # LookupInfo(value_type=int, ...)
```

`path` — поле или связь, после любых связей (`"tournament__name"`, `"tags"`, `"pk"`). Результат
сопоставляет окончанию каждого оператора после `path` — `""` для простого равенства, `"icontains"`,
`"year__gte"` — его описание и содержит только операторы, которые диалект поддерживает. У самой
связи — операторы её ключа (`""`, `in`, `not`, `not_in`, `isnull`, `not_isnull`; у прямой связи с
ключом из одной колонки — ещё и сравнения этой колонки); у `pk` составного первичного ключа — `""` и
`in`.

Оператору может понадобиться и возможность самого подключения, на котором выполняется запрос, а не
только диалекта: `posix_regex`/`iposix_regex` требуют `features.supports_posix_regex` — в SQLite это
подключение, в адресе которого есть `?install_regexp_functions=true`. `get_lookups()` описывает
диалект и перечисляет их; запрос с таким оператором на подключении без этой возможности даёт
`UnSupportedError` до выполнения.

## <a id="get_ordering_info"></a>`get_ordering_info()`

```python
info = Event._meta.get_ordering_info("-tournament__name")
info.relations    # (Event.tournament,)
info.fields       # (Tournament.name,)
info.paths        # ("tournament__name",)
info.descending   # True
```

У `OrderingInfo` есть `name`, `model`, `relations`, `fields` (поля, по которым идёт сортировка),
`paths` (имена, по которым сортирует запрос), `transforms` (путь внутри значения JSON, массива или
диапазона либо часть даты: `get_ordering_info("created__year").transforms == ("year",)`),
`descending` и `crosses_to_many`. Прямая связь сортирует по своим колонкам ключа без
соединения таблиц (`"tournament"` → пути `("tournament_id",)`); собственный `pk` модели — по полям
ключа, у составного первичного ключа — по каждому, в порядке ключа (`("id", "version")`); обратная
связь или «многие-ко-многим» (`"tags"`) и `pk` связанной модели (`"author__pk"`) — по полям первичного
ключа связанной модели, прочитанным через названную связь. Неизвестное имя даёт `FieldError`:
`Unknown field nme for ordering: Event has no field 'nme'`.

## <a id="on-a-queryset"></a>У запроса

`QuerySet.get_lookup_info()`, `get_lookups()` и `get_ordering_info()` учитывают ещё и вычисляемые
значения запроса: ключ может начинаться с такого значения, и тогда он описывается типом этого
значения (`Count()` — целое число).

```python
queryset = Tournament.objects.annotate(event_count=Count("events"))
queryset.get_lookup_info("event_count__gte").value_type    # int
```

## <a id="before-the-connections-are-set-up"></a>Пока подключения не настроены

Для любого описания нужны только модели, подключения не нужны. `Hare.bind_models()`
привязывает модели всей конфигурации — связи между ними, заменяемые модели, фильтры и сортировки
каждой модели — синхронно и не оставляет текущего контекста Hare:

```python
from hare import Hare

Hare.bind_models(config=CONFIG)  # та же конфигурация, что принимает Hare.init()

Book._meta.get_lookup_info("author__name__icontains").value_type  # str
Book.objects.all().get_ordering_info("-published_at").descending           # True
```

Он принимает конфигурацию в любом виде, который принимает `Hare.init()` (словарь, `HareConfig`, путь
к файлу или `"module.VARIABLE"`), и описывает все ключи, которые описывает полный `Hare.init()`.
Фреймворк, который строит сигнатуры и схемы обработчиков запросов при создании приложения, ещё до
того, как при запуске откроются подключения, читает фильтры отсюда. `Hare.init()` — или
`HareContext.init()` в любом контексте, в том числе с глобальным запасным контекстом, — позже
настраивает подключения как обычно.

`Model._meta.is_bound` показывает, привязана ли модель: `False` сразу после объявления класса и
`True`, как только его привяжет любой из этих вызовов:

```python
Book._meta.is_bound  # False
Hare.bind_models(config=CONFIG)
Book._meta.is_bound  # True
```

До этого любое описание даёт `ConfigurationError` (см. [Ошибки](#errors)). Запрос, созданный до
привязки (`BOOKS = Book.objects.all()` на уровне модуля), описывает свои ключи, как только модель будет
привязана.

## <a id="validation-in-filter-and-order-by"></a>Проверка в `.filter()` и `.order_by()`

`.filter()`, `.exclude()` и `.order_by()` проверяют каждый ключ при вызове — весь путь через связи,
поле и оператор, в том числе ключи внутри объектов `Q`:

```python
Event.objects.filter(tournament__nme="T")
# FieldError: Event.objects.filter(tournament__nme=...): Tournament has no field 'nme'
```

Запрос, построенный до `Hare.init()`, проверяется, когда `init()` проходит по нему заново, и `init()`
сообщает обо всех ошибочных запросах вместе с местом, где каждый был создан.
