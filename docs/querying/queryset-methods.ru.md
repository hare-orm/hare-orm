# Методы QuerySet

Все методы `QuerySet` — что каждый возвращает, выполняет и какие ошибки даёт, — а затем то, что не
помещается в строку таблицы: порядок `NULL` при сортировке, `Prefetch`, поток строк, рекурсивные запросы,
выборка из таблицы, записанные строки, вставка строк запроса и слияние.

## <a id="method-reference"></a>Список методов

Обозначения: **цепочка** — возвращает новый `QuerySet` (ничего ещё не выполняется); **await** —
возвращает особый объект, который нужно дождаться через `await`; **конечный** — сам
`QuerySet`/`QuerySetSingle` становится объектом для `await`.

Запрос, не закреплённый за подключением через `using()`, выбирает подключение при каждом выполнении.
Поэтому один и тот же объект запроса можно снова выполнить после окончания транзакции, внутри другой
транзакции или из другой задачи — каждый раз используется подключение (или открытая транзакция), текущее
в этот момент.

| Метод | Тип | Что делает |
|---|---|---|
| <code>filter(&#42;args: Q &#124; Exists, &#42;&#42;kwargs)</code> | цепочка | Копирует запрос и добавляет условия через AND — см. [`filter()`](#filter) |
| <code>exclude(&#42;args: Q &#124; Exists, &#42;&#42;kwargs)</code> | цепочка | То же, но отбрасывает строки, которые подходят под **все** условия вызова вместе: `exclude(a=1, b=2)` — это `NOT (a = 1 AND b = 2)`, то же, что `exclude(Q(a=1, b=2))` — см. [`exclude()`](#exclude) |
| <code>annotate(&#42;&#42;kwargs: Expression &#124; Term)</code> | цепочка | Добавляет в `SELECT` вычисляемые значения и агрегаты, по которым потом можно фильтровать — см. [`annotate()`](#annotate) |
| `alias(**kwargs)` | цепочка | Как `annotate()` — выражение можно использовать в `.filter()`/`.order_by()`, — но в `SELECT` оно попадает, только если `.values()`/`.values_list()` попросят его по имени. `FieldError`, если имя совпадает с полем модели или с `pk`. |
| <code>order&#95;by(&#42;orderings: str &#124; Expression &#124; Ordering)</code> | цепочка | Сортирует строки: `'-field'` — по убыванию, путь JSON, часть даты или `Ordering` — см. [`order_by()`](#order-by) |
| `orderings -> tuple[tuple[str, Order], ...]` | свойство | Сортировка, заданная через `order_by()`, — по одной паре `(path, Order)` на каждую колонку сортировки: `pk` означает все поля первичного ключа, прямая связь — колонки её ключа (`"author"` → `"author_id"`), `F("rating").desc(nulls_last=True)` — `("rating", Order.DESC_NULLS_LAST)` — см. [`orderings`](#orderings) |
| `after_cursor(*values)` | цепочка | Постраничный вывод по курсору (по значениям ключа сортировки) вместо `OFFSET` — нужен `.order_by()`; поддерживает `related__field` в сортировке (связь соединяется так же, как в `order_by`), но не вычисляемое значение — см. [`after_cursor()`](#after-cursor) |
| `before_cursor(*values)` | цепочка | Предыдущая страница: строки строго **до** значений — см. [`before_cursor()`](#before-cursor) |
| `cursor_values(instance) -> tuple` | синхронно | Значения курсора объекта `instance` для текущего `.order_by()` — передавайте их в `after_cursor()`/`before_cursor()` — см. [`cursor_values()`](#cursor-values) |
| `none()` | цепочка | Заведомо пустой запрос — ничего не отправляет в базу (`count`/`exists`/`values`/`delete`/`update`/... отвечают без запроса). |
| `limit(limit)` / `offset(offset)` | цепочка | `QueryError`, если это не целое число от 0 до 2⁶³−1 (`True` и `2.5` тоже не принимаются) — см. [`limit()` / `offset()`](#limit-offset) |
| `distinct(*field_names)` | цепочка | Без аргументов — обычный `DISTINCT` — см. [`distinct()`](#distinct) |
| `union(*other_querysets, all=False)` | цепочка | Возвращает QuerySet объединённых строк — тот же класс `QuerySet`, поэтому всё описанное ниже — обычный API QuerySet — см. [`union()`](#union) |
| `with_cte(name, query)` | цепочка | Добавляет именованный `WITH` (общее табличное выражение, CTE), не меняя собственный `FROM` запроса; `filter(id__in=CteRows(name, "id"))` берёт строки запроса из него — см. [Рекурсивные запросы и CTE](#recursive). |
| `with_recursive(relation, *, max_depth=None)` | цепочка | Строки, достижимые от строк запроса по связи `relation` — прямой, обратной или «многие-ко-многим» связи модели с самой собой, — шаг за шагом, вместе с самими строками, одним запросом `WITH RECURSIVE`: потомки, предки, связанные строки графа. См. [Рекурсивные запросы и CTE](#recursive). |
| `sample(percent, *, method=TableSampleMethod.BERNOULLI, seed=None)` | цепочка | Читает случайную выборку таблицы модели вместо всей таблицы — `TABLESAMPLE`. См. [Выборка из таблицы](#sample). |
| `select_for_update(*, nowait=False, skip_locked=False, of=(), no_key=False, share=False, key_share=False)` | цепочка | `SELECT ... FOR UPDATE` — см. [`select_for_update()`](#select-for-update) |
| `group_by(*fields)` | цепочка | До или после `.values()`/`.values_list()`, которые затем возвращают по строке на группу — см. [`group_by()`](#group-by) |
| `values_list(*field_names, flat=False, named=False, **expressions)` | цепочка | QuerySet кортежей (отдельных значений с `flat=True`, именованных кортежей с `named=True`) — см. [Запросы `values()` / `values_list()`](values.ru.md) — см. [`values_list()`](#values-list) |
| `values(*field_names, **expressions)` | цепочка | QuerySet словарей — см. [Запросы `values()` / `values_list()`](values.ru.md) — см. [`values()`](#values) |
| `delete()` | await | `DeleteQuery`, возвращает число строк — см. [`delete()`](#delete) |
| `hard_delete()` | await | Удаляет все подходящие строки по-настоящему, даже если задано `Meta.soft_delete_field`, — в том числе уже мягко удалённые, если запрос их видит (`Model.objects.only_deleted().hard_delete()` очищает корзину); возвращает число строк, `None` в ClickHouse — см. [`hard_delete()`](#hard-delete) |
| `restore(*, cascade=False)` | await | Отменяет мягкое удаление строк запроса — тех из них, что мягко удалены, при любой видимости (`Model.objects.filter(...).restore()` читает удалённые строки); возвращает число восстановленных строк, `None` в ClickHouse — см. [`restore()`](#restore) |
| `update(**kwargs)` | await | `UpdateQuery`, возвращает число строк — см. [`update()`](#update) |
| `update(**kwargs).returning(*fields)` / `delete().returning(*fields)` / `hard_delete().returning(*fields)` | await | Запись, возвращающая записанные строки — словарь полей `fields` на каждую строку или объекты модели, если поля не названы. См. [Записанные строки](#returning). |
| `count()` | await | Возвращает `int` — число строк, которое возвращает `await qs`, в том числе с агрегатными вычисляемыми значениями, фильтром `HAVING`, `.distinct()` или сортировкой через связь «ко многим» (её `JOIN` возвращает строку на каждую связанную строку) — см. [`count()`](#count) |
| `aggregate(**kwargs)` | await | Один словарь с ключом по имени каждого аргумента, по всему запросу (после `.group_by()` — по сгруппированным строкам, см — см. [`aggregate()`](#aggregate) |
| `exists()` | await | Возвращает `bool` — вернёт ли `await qs` хотя бы одну строку, с учётом среза. |
| `contains(obj)` | await | `ContainsQuery` — есть ли этот объект (по первичному ключу) в результате запроса? `QueryError`, если у `obj` нет первичного ключа. |
| `in_bulk(id_list=None, *, field_name="pk")` | `async def` | Объекты запроса в словаре по их значению поля `field_name` — только те, что хранят значение из `id_list`, если он передан (пустой даёт `{}` без запроса; длинный список — один связанный параметр — см. [Длинные списки `__in`](filters.ru.md#large-in-lists)) — см. [`in_bulk()`](#in-bulk) |
| `all()` | цепочка | Копия запроса без изменений. |
| `raw(sql, parameters=())` | await | Выполняет SQL как есть; `parameters` подставляются как настоящие параметры запроса на место `%s` (защита от SQL-инъекций: значения никогда не вставляются в текст запроса). `.sql()` возвращает исходную строку без изменений, с неподставленными `%s`. |
| `first()` / `last()` | конечный | Первая или последняя строка по сортировке — см. [`first()` / `last()`](#first-last) |
| `latest(*orderings)` / `earliest(*orderings)` | конечный | Сортируют по убыванию/возрастанию по данным полям — без них по `Meta.get_latest_by` модели, — затем `first()`; `NULL` никогда не побеждает (`NULLS LAST` на любом диалекте — см. [Порядок NULL при сортировке](#null-ordering)). `FieldError`, если нет ни того, ни другого. |
| `reverse()` | цепочка | Сортировка, развёрнутая точно — и направление, и явное место `NULL`: заданная `order_by()`, иначе `Meta.ordering`; неупорядоченный QuerySet остаётся неупорядоченным. `QueryError` на срезе и на `union()`. |
| `dates(field_name, trunc_type, order="ASC")` | цепочка | Различные усечённые даты поля даты — см. [`dates()`](#dates) |
| `datetimes(field_name, trunc_type, order="ASC", tzinfo=None)` | цепочка | То же для `DatetimeField`, как `datetime` с часовым поясом; `trunc_type` также `hour`, `minute` или `second`; усечение в поясе `tzinfo` (имя IANA или `ZoneInfo`; по умолчанию текущий пояс). Другое поле, единица или порядок дают `FieldError`. |
| `get(*args, does_not_exist_exception=..., multiple_objects_returned_exception=..., **kwargs)` | конечный | Единственный подходящий объект — см. [`get()`](#get) |
| `create(**kwargs)` | `async def` | Создаёт строку и возвращает её объект — `Model(**kwargs)`, сохранённый на подключении запроса (`Model.objects.using("replica").create(...)`); фильтры запроса не участвуют — см. [`create()`](#create) |
| `get_or_create(defaults=None, **kwargs)` | `async def` | Объект, подходящий под `kwargs` среди строк запроса, либо новый, созданный из `kwargs` и `defaults`, — `(object, created)` — см. [`get_or_create()`](#get-or-create) |
| `update_or_create(defaults=None, create_defaults=None, **kwargs)` | `async def` | Обновляет объект, подходящий под `kwargs`, значениями `defaults` либо создаёт его с `create_defaults` (с `defaults`, если они не заданы) — `(object, created)` — см. [`update_or_create()`](#update-or-create) |
| `bulk_create(objects, batch_size=None, *, ignore_conflicts=False, update_fields=None, on_conflict=None, on_conflict_constraint=None, conflict_where=None, returning=None, use_copy=False)` | await | Возвращает объекты в том виде, в каком их передали (и объекты генератора) — вместе с теми, что обновлены вставкой-или-обновлением; с `ignore_conflicts=True` — те, что вставлены, — см. [`bulk_create()`](#bulk-create) |
| `insert_from(queryset, *, fields)` | await | Записывает строки, которые выбирает запрос, в таблицу модели одним `INSERT ... SELECT` и возвращает число записанных строк. См. [Вставка строк запроса](#insert-from). |
| `merge(source, *, on)` | await | Один `MERGE`, сопоставляющий список строк или запрос со строками модели и по веткам обновляющий, удаляющий или вставляющий их. См. [Слияние строк](#merge). |
| `refresh_materialized_view(name, *, concurrently=False)` | await | Заново наполняет материализованное представление из `Meta.materialized_views` строками его запроса на подключении, в которое пишет модель. См. [Материализованные представления](../models/schema-objects.ru.md#materialized-views). |
| `get_next_sequence_value(name)` | await | Следующий номер последовательности из `Meta.sequences`. См. [Последовательности](../models/schema-objects.ru.md#sequences). |
| `bulk_update(objects, fields, batch_size=None, *, returning=None)` | await | Не поддерживает поля «многие-ко-многим» и обратных связей (`FieldError`) и значения `F()`/выражения (`QueryError` — используйте `.update()` или `save()`); требует первичный ключ у каждого объекта — см. [`bulk_update()`](#bulk-update) |
| `only(*fields)` | цепочка | Загружает только перечисленные поля — получается частично загруженная модель — см. [`only()`](#only) |
| `defer(*fields)` | цепочка | Не загружает перечисленные поля самой модели — имя прямой связи откладывает её ключевые поля, как `only()` по нему их загружает; повторные вызовы складываются. Чтение отложенного поля даёт `NoValuesFetched`, как после `only()`. `related__field` не поддерживается. Несовместим с `.only()`. |
| <code>select&#95;related(&#42;args: str &#124; Select)</code> | цепочка | Загружает связанные объекты в том же запросе через `JOIN`; принимает строку или `Select(relation, extra_condition=Q(...))` — см. [`select_related()`](#select-related) |
| <code>prefetch&#95;related(&#42;args: str &#124; Prefetch &#124; None)</code> | цепочка | Загружает связанные объекты отдельными запросами (не через `JOIN` с выбираемыми строками); принимает строку (в том числе `"a__b"`) или `Prefetch(...)` — см. [`prefetch_related()`](#prefetch-related) |
| `defer_related(*fields)` | цепочка | Отключает для этого запроса автоматическую загрузку связей с `lazy=RelationLoadStrategy.JOINED`/`.SELECT`. |
| `include_deleted()` / `only_deleted()` | цепочка | Мягко удалённые строки вместе с живыми / только мягко удалённые — вместо фильтра [`Meta.soft_delete_field`](../soft-delete-versions-tenants/soft-delete.ru.md) по умолчанию — см. [`include_deleted()` / `only_deleted()`](#include-deleted-only-deleted) |
| `all_tenants()` | цепочка | Строки всех арендаторов вместо фильтра [`Meta.tenant_field`](../soft-delete-versions-tenants/multi-tenancy.ru.md#all-tenants) по умолчанию — в том числе для моделей с арендатором, до которых запрос доходит через связи. Сочетается с `include_deleted()`/`only_deleted()` в любом порядке. |
| `explain(output_format=None, **options)` | `async def` | Выполняет `sql(explain=True)` (`EXPLAIN QUERY PLAN` в SQLite) и возвращает строки плана словарями «колонка — значение» (`{"QUERY PLAN": ...}` в PostgreSQL, `{"explain": ...}` в ClickHouse), одинаковыми у драйверов одной базы. `output_format=` и `**options` — только PostgreSQL; SQLite даёт для них `UnSupportedError`. |
| `using(using)` | цепочка | Закрепляет запрос за конкретным подключением — по имени подключения (`"replica"`) или по клиенту (клиент транзакции, `Connections.get(connection_alias)`); `None` снимает закрепление — см. [Несколько баз данных](../connections/multiple-databases.ru.md#using-db). |
| `get_connection(*, for_write=False)` | синхронно | Подключение, на котором запрос выполнился бы сейчас — см. [`get_connection()`](#get-connection) |
| `iterator(chunk_size=1000)` | асинхронный генератор | Читает строки страницами — см. [`iterator()`](#iterator) |
| `stream(chunk_size=1000)` | асинхронный генератор | Настоящий курсор — на стороне сервера в PostgreSQL, курсор, читаемый партиями, в SQLite — внутри транзакции. См. ниже. |
| `sql(parameters_inline=False, *, explain=False, output_format=None, **explain_options)` | синхронно | Возвращает SQL, который запрос выполнит на своём подключении, — с местами для параметров или с подставленными значениями; `explain=True` возвращает вместо него команду `EXPLAIN`. Несколько команд (массовая запись) соединяются через `;`. |
| `get_read_columns()` | синхронно | Возвращает колонки, которые читает запрос, — выбранные, в условиях, соединениях и подзапросах — парами `(таблица, колонка)`, взятыми из самого запроса, а не из его SQL; от них зависит [представление](../models/schema-objects.ru.md#views) этого QuerySet. |

`await qs` возвращает `list[Model]` (или `Model | None`, если `.first()`/`.get()`/... сузили результат до
одной строки); `async for x in qs` тоже работает.

## <a id="methods"></a>Методы

### <a id="filter"></a>`filter()`

```python
class QuerySet:
    def filter(self, *args: Q | Exists, **kwargs: Any) -> Self: ...
```

Копирует запрос и добавляет условия через AND. Поддерживает `related__field=...`, позиционные объекты `Q`
и условия `Exists(...)`. Запрос со срезом (`.limit()`/`.offset()`/`qs[a:b]`) фильтровать нельзя:
`filter()`/`exclude()`/`get(**kwargs)` на нём дают
`QueryError("Cannot filter a query once a slice has been taken.")` — сначала фильтр, потом срез. Запрос
`.values()`/`.values_list()` (или операция над множествами) в роли значения фильтра (`id__in=...`) должен
выбирать одну колонку — если больше, будет
`QueryError("Cannot use multi-field values as a filter value...")`, а не ошибка базы.

### <a id="exclude"></a>`exclude()`

```python
class QuerySet:
    def exclude(self, *args: Q | Exists, **kwargs: Any) -> Self: ...
```

То же, но отбрасывает строки, которые подходят под **все** условия вызова вместе: `exclude(a=1, b=2)` —
это `NOT (a = 1 AND b = 2)`, то же, что `exclude(Q(a=1, b=2))`. Для связи «ко многим» условия одного
вызова `exclude()` (или одного `~Q(...)`) относятся к **одной и той же** связанной строке, как в
`filter()` — `exclude(books__name="a", books__rating=5)` отбрасывает автора, только если одна книга
подходит под оба условия, и оставляет автора, у которого одна книга называется «a», а другая имеет
оценку 5. Чтобы отбросить авторов, у которых любая книга подходит под любое из условий, используйте
отдельные вызовы — `.exclude(books__name="a").exclude(books__rating=5)` отбрасывает авторов, у которых
любая книга подходит под любое из условий. Каждый отдельный `.exclude()` отбрасывает свои строки.
`exclude()` через связь «ко многим» — это подзапрос `NOT EXISTS`, поэтому он не добавляет в запрос
`JOIN`: следующий `.filter(books__...)` всё равно строит `JOIN`, который читают `values("books__...")`,
`order_by("books__...")` и `Count("books")`. Пустой `Q()` здесь, как и везде, ничего не делает — `~Q()` и
`exclude(Q())` подходят под каждую строку. Как обрабатываются колонка или значение `NULL` — см.
[Как работает NULL](filters.ru.md#null-semantics) ниже.

### <a id="annotate"></a>`annotate()`

```python
class QuerySet:
    def annotate(self, **kwargs: Expression | Term) -> Self: ...
```

Добавляет в `SELECT` вычисляемые значения и агрегаты и регистрирует для них операторы фильтра, чтобы
потом по ним можно было вызывать `.filter()`/`.exclude()` (условие попадает в `HAVING`, если в нём есть
агрегат, — в том числе в выражении, смешивающем агрегат с колонкой, вроде `Count("books") + F("id")`, или
когда поле сравнивается с агрегатным вычисляемым значением, `budget__lt=F("total")`; иначе — в `WHERE`).
Имя, совпадающее с полем модели (или `pk`), даёт `FieldError`, как и такое имя для выражения в
`alias()`/`values()`/`values_list()`: иначе все остальные обращения к нему читали бы вычисляемое значение
вместо поля. Так же отклоняется имя связи, атрибута класса модели (`save`, `delete`, `objects`, `_meta`)
или имя, начинающееся с `_` (собственное состояние объекта), — у каждого прочитанного объекта на его
месте оказалось бы значение аннотации. Пустое имя даёт `QueryError`, и в `alias()` и `aggregate()` тоже.
Значение, которое не является выражением (например, просто строка с именем поля — используйте
`F("field")`), даёт `TypeError`, и в `alias()` тоже. `.alias()`, которое ничто не читает — ни фильтр, ни
сортировка, ни группировка, ни другое вычисляемое значение, ни поле `values()`, — в запрос не попадает,
поэтому его `JOIN` «ко многим» не размножает строки.

### <a id="order-by"></a>`order_by()`

```python
class QuerySet:
    def order_by(self, *orderings: str | Expression | Ordering) -> Self: ...
```

`'-field'` — по убыванию (`DESC`); путь внутрь поля JSON или вычисляемого значения (`'-data__score'`)
сортирует по значению JSON, в собственном порядке базы (см. [`F("data__key")`](expressions.ru.md)); [часть
даты](values.ru.md#date-part-paths) (`'-created__month'`) сортирует по этой части;
`F('field').asc()`/`.desc()` с `nulls_first=True`/`nulls_last=True` задают, где окажутся `NULL`, — см.
[Порядок NULL при сортировке](#null-ordering). Выражение сортирует по своему значению — `order_by(F('price')
* F('quantity'))`, `order_by(Lower('name').desc(nulls_last=True))` (у каждого выражения есть
`.asc()`/`.desc()`), в `latest()`/`earliest()` тоже — через собственный псевдоним, который не выбирается, —
как и выражение из `Meta.ordering`; константа (`Value(1)`, вычисляемое значение из неё) строки не
упорядочивает и в `ORDER BY` не пишется — база прочитала бы число там как номер колонки; `union()`
принимает только имена (иначе `QueryError`). Поддерживает
`related__field`; имя прямой связи (`'author'`), в том числе в конце пути через связь (`'book__author'`),
сортирует по колонкам её ключа (`author_id`, `book__author_id`), и в `Meta.ordering` тоже; имя связи «ко
многим» или обратной «один-к-одному» (`'events'`, `'-tags'`) сортирует по первичному ключу связанной модели
(по строке на каждую связанную строку), а `pk` в конце пути (`'author__pk'`, `'tags__pk'`) — по полям
первичного ключа — см. [связи и `pk` в путях к полям](values.ru.md#relation-field-paths). Нельзя вызывать
после `.after_cursor()`. Запрос со срезом (`qs[a:b]`, `.limit()`, `.offset()`, индекс) нельзя
пересортировать — `QueryError("Cannot reorder a query once a slice has been taken.")`; сначала сортировка,
потом срез (`order_by("name").limit(2)`). То же относится к `union()` и к операции над множествами из
`.values()`/`.values_list()`. `order_by()` без аргументов убирает всю сортировку, включая `Meta.ordering`.
`'?'` упорядочивает строки случайно — по `Random()`, новому числу для каждой строки, отдельно или рядом с
другими сортировками (`order_by('category', '?')`); база читает все строки, чтобы их отсортировать, поэтому
на большой таблице выбирайте строки иначе.

### <a id="orderings"></a>`orderings`

Сортировка, заданная через `order_by()`, — по одной паре `(path, Order)` на каждую колонку сортировки:
`pk` означает все поля первичного ключа, прямая связь — колонки её ключа (`"author"` → `"author_id"`),
`F("rating").desc(nulls_last=True)` — `("rating", Order.DESC_NULLS_LAST)`. Пусто без `order_by()` (тогда
запрос сортируется по `Meta.ordering`, которая сюда не входит, — читайте `Model._meta.ordering`) и после
`order_by()` без аргументов, который оставляет запрос несортированным. `before_cursor()` оставляет его
как задано; копия запроса его сохраняет, следующий `order_by()` заменяет. `Order` находится в `hare.sql`.

### <a id="after-cursor"></a>`after_cursor()`

```python
class QuerySet:
    def after_cursor(self, *values: Any) -> Self: ...
```

Постраничный вывод по курсору (по значениям ключа сортировки) вместо `OFFSET` — нужен `.order_by()`;
поддерживает `related__field` в сортировке (связь соединяется так же, как в `order_by`), но не
вычисляемое значение. У запроса с `.distinct(<fields>)` граница применяется к строкам, которые выбирает
`DISTINCT ON` (через `pk IN (SELECT ...)`), как и у `before_cursor()`. Строки берутся строго после
значений: для обхода страниц нужна сортировка, значения которой не совпадают у двух строк, —
заканчивающаяся первичным ключом (`order_by("-created", "pk")`) или уникальным полем без `NULL`, — иначе
строки с теми же значениями, что у последней строки страницы, пропускаются, а их порядок внутри страницы
тоже не определён. `iterator()` добавляет первичный ключ сам.

### <a id="before-cursor"></a>`before_cursor()`

```python
class QuerySet:
    def before_cursor(self, *values: Any) -> Self: ...
```

Предыдущая страница: строки строго **до** значений. Выполняется в точно обратной сортировке (поэтому
`.limit(n)` берёт `n` строк, ближайших к курсору) и возвращает строки в исходном порядке `.order_by()` —
для объектов, `values()` и `values_list()` одинаково; `first()`/`get()` берут строку прямо перед
курсором. Ошибки те же, что у `after_cursor()`. Вместе с `after_cursor()` (в любом порядке) задаёт окно;
повторный вызов любого из них заменяет только свою границу. Не поддерживается в `iterator()`/`stream()`.

### <a id="cursor-values"></a>`cursor_values()`

```python
class QuerySet:
    def cursor_values(self, obj: TModel) -> tuple[Any, ...]: ...
```

Значения курсора объекта `instance` для текущего `.order_by()` — передавайте их в
`after_cursor()`/`before_cursor()`. Сортировка `related__field` требует загруженной связи
(`select_related()`), иначе `QueryError`; связь `NULL` даёт `None`; сортировка по внешнему ключу или
«один-к-одному» (`'city__country'`) даёт значение ключа, а не связанный объект; связь «ко многим» или
вычисляемое значение дают `FieldError`.

### <a id="limit-offset"></a>`limit()` / `offset()`

```python
class QuerySet:
    def limit(self, limit: int) -> Self: ...

    def offset(self, offset: int) -> Self: ...
```

`QueryError`, если это не целое число от 0 до 2⁶³−1 (`True` и `2.5` тоже не принимаются).
`qs[start:stop]` — то же, что `.offset(start).limit(stop-start)` (шаг только `1`/`None`, `start`/`stop`
не отрицательные); `stop`, не превышающий `start` (`qs[3:3]`, `qs[4:2]`), не выбирает строк, как срез
списка. Смещение без ограничения записывается в SQLite как `LIMIT -1 OFFSET n` (SQLite не принимает
`OFFSET` без `LIMIT`), и для запросов, и для объединений. `qs[i]` — один объект с индексом `i`; если его
нет, при `await` будет `IndexError`; отрицательный индекс даёт `QueryError`. Объединение принимает срезы
и индекс так же.

### <a id="distinct"></a>`distinct()`

```python
class QuerySet:
    def distinct(self, *args: str) -> Self: ...
```

Без аргументов — обычный `DISTINCT`. С аргументами — по одной строке на сочетание полей: поле, связь,
вычисляемое значение или [часть даты](values.ru.md#date-part-paths) (`"created__date"`), первая в
сортировке: `order_by` должен начинаться с тех же полей в том же порядке; `count()`/`exists()` (и со
срезом) считают строки, которые он выбирает; `aggregate()` даёт `QueryError`. Выполняется
`DISTINCT ON (fields)` (какую строку сочетания оставить, решает база, если сортировка не продолжается за
этими полями); база без него (SQLite, ClickHouse — `Features.supports_distinct_on` выключен) даёт
`UnSupportedError` при построении запроса. Простой `distinct()` запроса, отсортированного по невыбранному
полю, выбирает и колонки сортировки, поэтому строки, различающиеся только ими, остаются
разными. Ветка
`union()` с `distinct(*fields)` сохраняет свою сортировку. `distinct()` (с полями или без) на запросе со
срезом даёт `QueryError("Cannot create distinct fields once a slice has been taken.")` — вызывайте его до
среза.

### <a id="union"></a>`union()`

```python
class QuerySet:
    def union(self, *other_querysets: QuerySet[Any, Any], all: bool = False) -> Self: ...
```

Возвращает QuerySet объединённых строк — тот же класс `QuerySet`, поэтому всё описанное ниже — обычный
API QuerySet. Цепочка вызовов `union()`/`union(all=True)`/`intersection()`/`difference()` объединяет
каждую новую ветку со всем результатом, полученным до неё, в порядке вызовов:
`a.union(b).union(c, all=True)` — это `(a UNION b) UNION ALL c` (повторы, которые приносит `c`,
остаются), `a.union(b, all=True).union(c)` убирает все повторы, а `a.union(b).intersection(c)` — это
`(a UNION b) INTERSECT c` на любой базе (производная таблица не даёт более высокому приоритету
`INTERSECT` в PostgreSQL изменить смысл). У каждой ветки может быть свой `.annotate()`, если имя не
совпадает с настоящим полем модели. Вычисляемое значение раскодируется через поле первой ветки, у которой
оно есть: у ветки с `Value(None)` поля нет, и она ничего не меняет (`date` в SQLite остаётся `date`), а
не раскодированным значение остаётся, только если у значения другой ветки поля нет вовсе (например,
`Value("text")`). `.order_by()` у объединения ссылается на выбранные колонки по номеру, поэтому работает
на любой базе и с ветками, соединяющими связь или группирующими по агрегату.
`select_related()`/`prefetch_related()` у отдельной ветки (до
`.union()`/`.intersection()`/`.difference()`) по-прежнему дают `QueryError`: загруженная связь не
переживёт слияния в общий результат. Вызывайте `.prefetch_related()` у самого объединённого QuerySet: он
выполняет предзагрузку партией по уже полученному списку результата через `prefetch_related_objects()`,
после выполнения, и принимает то же (`str`/`Prefetch(...)`), что `QuerySet.prefetch_related()`. Ветка
сама может быть операцией над множествами — `a.union(b.intersection(c))` объединяет `b INTERSECT c`
целиком. Ветку — и вложенную операцию тоже — можно отсортировать и взять у неё срез
(`a.order_by("-rank")[:3].union(b)`): она объединяется как производная таблица своих строк. Все ветки
выполняются на одном подключении — ветка, чья модель или маршрутизатор выбирает другое подключение, чем у
первой ветки, даёт `QueryError`. Объединение работает как значение `__in`, как запрос:
`filter(pk__in=a.union(b))`, `filter(author__in=a.union(b))` сравнивают с первичным ключом его строк.
Получение результата работает, как у запроса: `count()`, `exists()` (со срезом), `first()`/`last()` (без
сортировки — по первичному ключу; `last()` у объединения со срезом даёт `QueryError`),
`get(*args, does_not_exist_exception=..., multiple_objects_returned_exception=..., **kwargs)` (как у
обычного запроса), `iterator(chunk_size=1000)` (страницы через `OFFSET`, в порядке `order_by()`, а затем
всех выбранных колонок — и для объединения нескольких моделей ещё и модели — чтобы равные строки шли в
постоянном порядке; срез ограничивает перебор; `prefetch_related()` выполняется для каждой страницы) и
`stream(chunk_size=1000)` (курсор, как [`QuerySet.stream()`](#stream), — внутри транзакции, без
`prefetch_related()`). `get()` принимает и условия: условия применяются к каждой ветке до объединения
(условие на строку оставляет те же строки до и после `UNION`/`INTERSECT`/`EXCEPT`), а на объединении со
срезом дают `QueryError` — его строки зависят от строк, которые условие отбрасывает. `aggregate()`
считает по объединённым строкам. Методы, меняющие то, какие строки выбирает запрос, — `filter()`,
`exclude()`, `annotate()`, `alias()`, `distinct()`, `group_by()`, `only()`, `defer()`,
`select_related()`, `select_for_update()`, `with_cte()`, курсоры, `all_tenants()`, `include_deleted()`,
`only_deleted()` — и записи (`update()`/`delete()`/`hard_delete()`/`create()`/...) у самого объединения
дают `QueryError` — применяйте их к веткам. Запросы к моделям объединяются с запросами к моделям, а
запросы `.values()`/`.values_list()` — друг с другом (смесь — `QueryError` — см.
[Запросы `values()` / `values_list()`](values.ru.md)); `.values()`/`.values_list()` у объединения
запросов к моделям превращают его в объединение значений.

### <a id="select-for-update"></a>`select_for_update()`

```python
class QuerySet:
    def select_for_update(
        self,
        *,
        nowait: bool = False,
        skip_locked: bool = False,
        of: Sequence[str] = (),
        no_key: bool = False,
        share: bool = False,
        key_share: bool = False,
    ) -> Self: ...
```

`SELECT ... FOR UPDATE`. Сила блокировки — одно из: по умолчанию `FOR UPDATE`, `no_key=True` —
`FOR NO KEY UPDATE` (строки, ссылающиеся на заблокированные, можно записывать; на базе без неё — обычный
`FOR UPDATE`), `share=True` — `FOR SHARE` (другие транзакции могут взять ту же блокировку, но никто не
может обновить или удалить строки), `key_share=True` — `FOR KEY SHARE` (ждут только удаление и изменение
ключа); больше одного флага даёт `QueryError`; `FOR SHARE`/`FOR KEY SHARE` на базе
без них (`features.supports_select_for_share`/`supports_select_for_key_share`) дают `UnSupportedError`
при выполнении запроса. Строки, которые читает `prefetch_related()` этого запроса, блокируются так же.
Если запрос выполняется на базе без `FOR UPDATE` (`features.supports_select_for_update` ложно, например
SQLite), даёт `UnSupportedError`: молча пропустить вызов значило бы позволить вызывающему думать, что
строки заблокированы, хотя это не так. На базе, которая это умеет, даёт `QueryError`, если запрос
выполняется вне блока `Transactions.atomic()`: иначе блокировка была бы взята и тут же снята
(автоматическая фиксация), не давая настоящей защиты, но создавая её видимость. `of` принимает `"self"`
(или имя таблицы самой модели) и пути прямых связей в том виде, в каком их принимает `select_related()`
(`"author"`, `"author__country"`); в SQL они становятся псевдонимами, под которыми запрос соединяет
таблицы. Связь, названная в `of`, соединяется через `INNER JOIN` вместо `LEFT OUTER JOIN` (PostgreSQL не
может блокировать сторону внешнего соединения, допускающую `NULL`), поэтому каждый шаг её пути должен
быть внешним ключом `NOT NULL` с `db_constraint=True` и без условий мягкого удаления, арендатора или
`Select(extra_condition=...)`; иначе, а также для имени, которое не является таким путём или не
соединяется запросом, будет `QueryError`. Без `of` запрос с любым `JOIN` блокирует только строки самой
модели. Запрос с агрегатным вычисляемым значением или `GROUP BY` даёт `QueryError`: сгруппированная
строка обозначает несколько строк таблицы, которые SQL заблокировать не может, — блокируйте строки
отдельным запросом. То же относится к запросу с `.distinct()`/`.distinct(<fields>)`, к запросу, читающему
оконную функцию (`Window(...)`), и к ветке `union()`/`intersection()`/`difference()` — в том числе
запросу `.values()`/`.values_list()`, — их PostgreSQL тоже заблокировать не может. `count()`, `exists()`
и `aggregate()` такого запроса тоже дают `QueryError`: строки, которые SQL обобщает, он не блокирует, а
молча выброшенная блокировка дала бы вызывающему думать, что строки заблокированы.

### <a id="group-by"></a>`group_by()`

```python
class QuerySet:
    def group_by(self, *fields: str | GroupingSet) -> Self: ...
```

До или после `.values()`/`.values_list()`, которые затем возвращают по строке на группу. Поля — это
имена полей или вычисляемых значений: выражение (`F("id") + 1`, `Count(...)`) даёт `QueryError` —
сначала добавьте его через `annotate()` и группируйте по имени. Объекты моделей
всегда группируются по первичному ключу, поэтому на запросе объектов строки не меняются — `await qs`,
`count()`, `exists()`, `contains()`, `update()` и `delete()` видят по строке на объект. `aggregate()`
работает над сгруппированными строками (теми, что вернул бы `.values()`) и может читать только поля
группировки и вычисляемые значения (иначе `QueryError`):
`.annotate(n=Count("id")).group_by("author_id").aggregate(most=Max("n"))` — наибольшее число на одного
автора. Сгруппированный запрос `.values()`/`.values_list()` не учитывает `Meta.ordering` (её колонки не
сгруппированы) — без сортировки его строки идут в порядке базы, а `first()`/`last()` и `iterator()`
берут ключ группировки как порядок. Его выбранные поля, `order_by()` и
`HAVING` читают поля группировки, агрегаты и вычисляемые значения над ними — при группировке по
первичному ключу любую колонку модели; другую колонку база читает или отклоняет сама (см.
[Неявный `GROUP BY`](aggregation.ru.md#implicit-group-by)).

### <a id="values-list"></a>`values_list()`

```python
class QuerySet:
    def values_list(
        self,
        *fields_: str,
        flat: bool = False,
        named: bool = False,
        **kwargs: Expression | Term,
    ) -> QuerySet[TModel, Any, TAnnotations]: ...
```

QuerySet кортежей (отдельных значений с `flat=True`, именованных кортежей с `named=True`) — см.
[Запросы `values()` / `values_list()`](values.ru.md). Несовместим с `.only()`/`.defer()`. Без аргументов
— все поля и вычисляемые значения. Имя может быть путём внутрь поля JSON или вычисляемого значения
(`"data__owner__name"`), читаемым так же, как его читает `F()`, или
[частью даты](values.ru.md#date-part-paths) (`"created__year"`). `named=True` возвращает каждую строку
как именованный кортеж с атрибутом на каждое выбранное имя (имя, которое не является допустимым
идентификатором, становится `_<index>`); `flat` вместе с `named`, позиционный аргумент не-строка или
именованный аргумент, который не является выражением, дают `QueryError`. Полноценный запрос, возвращающий
кортежи, — см. [Запросы `values()` / `values_list()`](values.ru.md).

### <a id="values"></a>`values()`

```python
class QuerySet:
    def values(
        self,
        *args: str,
        **kwargs: str | Expression | Term,
    ) -> QuerySet[TModel, dict[str, Any], TAnnotations]: ...
```

QuerySet словарей — см. [Запросы `values()` / `values_list()`](values.ru.md). `kwargs` — пары
`alias="field_name"` или `alias=<expression>`; любое другое значение даёт `QueryError`. Имя может быть
путём внутрь поля JSON или вычисляемого значения (`"data__owner__name"`), читаемым так же, как его читает
`F()`, или [частью даты](values.ru.md#date-part-paths) (`"created__year"`). Несовместим с
`.only()`/`.defer()`. Полноценный запрос, возвращающий словари, — см.
[Запросы `values()` / `values_list()`](values.ru.md).

### <a id="delete"></a>`delete()`

```python
class QuerySet:
    def delete(self) -> DeleteQuery: ...
```

`DeleteQuery`, возвращает число строк — `None` на базе, которая его не сообщает
(`Features.reports_written_row_count` выключен: ClickHouse), если строки не удаляет сам hare; в SQLite это
строки, удалённые самой командой, без строк запроса,
которые раньше удалил каскад `FOREIGN KEY` той же команды (ссылка модели на себя:
`Node.objects.all().delete()` родителя и потомка даёт 1). Работает так же, как `Model.delete()`, но для
многих строк сразу: при заданном `Meta.soft_delete_field` превращается в `UPDATE` вместо настоящего
`DELETE` (одна команда, если на эту модель ничто не ссылается, иначе — вместе с тем же каскадом, что
выполняет `Model.delete()`); сразу даёт `ProtectedError`, если на найденную строку ещё ссылается связь с
`on_delete=PROTECT` (проверяется один раз для всего найденного набора, а не для каждой строки); сначала
выполняет каскад в Python, если у связи `db_constraint=False` (в базе нет настоящего `FOREIGN KEY`,
поэтому при обычном массовом `DELETE` её `on_delete` никто не выполнит). Каскад в Python идёт партиями в
одной транзакции: каждая волна затронутых строк стоит одного запроса на связь и модель (больше — только
сверх ограничения базы на число параметров), а не запроса на строку, и все строки, которые он мягко
удаляет, получают одно и то же `deleted_at`. Только если модель переопределяет `delete()`, каждая
найденная (или затронутая каскадом) строка удаляется через это переопределение по одной — связь, ведущая
к ней, даже та, которую поддерживает база, каскадируется в Python. Запрос со срезом (`qs[:10]`,
`.limit()`, `.offset()`) и запрос с `.distinct(<fields>)` дают `QueryError` (`Cannot delete() a query once
a slice has been taken.`, `Cannot call delete() after .distinct(*fields).`) до отправки SQL —
`DELETE` не принимает срез; удаляйте строки по ключам:
`Model.objects.filter(pk__in=Subquery(qs[:10].values("pk")))`. Фильтр по агрегатному вычисляемому
значению (`HAVING`, в том числе через `.alias()`/`.exclude()`) или через связь идёт через подзапрос
`pk IN (SELECT ...)`, поэтому удаляются только подходящие строки.

### <a id="hard-delete"></a>`hard_delete()`

```python
class QuerySet:
    def hard_delete(self) -> HardDeleteQuery: ...
```

Удаляет все подходящие строки по-настоящему, даже если задано `Meta.soft_delete_field`, — в том числе уже
мягко удалённые, если запрос их видит (`Model.objects.only_deleted().hard_delete()` очищает корзину).
Связанные строки следуют своему `on_delete` так же, как при `delete()` модели без мягкого удаления; у
модели без `Meta.soft_delete_field` это то же, что `delete()`. У модели с переопределённым
`hard_delete()` оно вызывается для каждой подходящей строки по одной. Результат ожидания — число
строк — `None` на базе, которая его не сообщает (`Features.reports_written_row_count` выключен: ClickHouse).

### <a id="restore"></a>`restore()`

```python
class QuerySet:
    def restore(self, *, cascade: bool = False) -> RestoreQuery: ...
```

Отменяет мягкое удаление строк запроса — тех из них, что мягко удалены, при любой видимости
(`Model.objects.filter(...).restore()` читает удалённые строки); возвращает число восстановленных строк —
`None` на базе, которая его не сообщает (`Features.reports_written_row_count` выключен: ClickHouse). Один
`UPDATE` — поля `auto_now` и `Meta.optimistic_lock_field` меняются как у `update()`. `cascade=True`
восстанавливает и строки, удалённые мягким удалением каждой из них вместе с ней (те, у которых ещё стоит её
время удаления, как у [`Model.restore(cascade=True)`](../models/model-methods.ru.md)), в одной транзакции; у модели с переопределённым
`restore()` он вызывается для каждой строки. `QueryError` для модели без `Meta.soft_delete_field`, запроса
`values()`, `union()` или `sample()`. Только строки активного арендатора, если не `all_tenants()`.

### <a id="update"></a>`update()`

```python
class QuerySet:
    def update(self, **kwargs: Any) -> UpdateQuery: ...
```

`UpdateQuery`, возвращает число строк — `None` на базе, которая его не сообщает
(`Features.reports_written_row_count` выключен: ClickHouse). Не принимает в `kwargs`
`Meta.soft_delete_field`/`Meta.optimistic_lock_field` (`QueryError`) — вместо этого `optimistic_lock_field`
каждой найденной строки увеличивается автоматически (`SET version = version + 1`), если `kwargs` не пуст, и
каждое поле `auto_now` получает одно и то же «сейчас», названо оно в `kwargs` или нет, — так же, как это
делает `save()`. В отличие от `save()`, проверки устаревания нет: у `.update()` нет ранее загруженного
объекта, с версией которого можно сравнить, поэтому он никогда не даёт `StaleObjectError`. Запрос со срезом и
запрос с `.distinct(<fields>)` дают `QueryError`, как в `delete()`; при фильтре по агрегатному вычисляемому
значению или через связь обновляются ровно строки, которые возвращает `await qs`, — через подзапрос `pk IN
(SELECT ...)`, а `bulk_update()` на таком запросе записывает только объекты, строки которых ему соответствуют.
Значение-агрегат (`Sum("qty")`) или значение, читающее агрегатное вычисляемое значение (`F("n")` после
`annotate(n=Count(...))`), даёт `FieldError`: в `UPDATE` нет групп для агрегата; `Subquery()`, агрегирующий
связанные строки, работает. Целое поле, заданное дробным значением (`update(quantity=F("price"))`), получает
собственное преобразование базы: PostgreSQL округляет (у `Decimal` половина — от нуля, у `float` — к чётному),
ClickHouse отбрасывает дробную часть, а в SQLite, чья колонка `INTEGER` сохранила бы дробь, hare записывает её
`CAST` SQLite с отброшенной дробной частью. Значение типа, который поле не хранит, даёт `FieldError`: текст в
число, число в логическое, а также байты, JSON, IP- или MAC-адрес, массив, словарь (`Map`), кортеж, вектор,
вектор полнотекстового поиска, геометрия, `hstore`, диапазон или `interval` PostgreSQL в поле другого типа
(`update(data=F("id"))` у `JSONField`), в том числе массив других элементов (`text[]` в `integer[]`) и
диапазон других границ (`int8range` в `int4range`); текстовое поле принимает любое значение как его текст.
Литерал, которым может оказаться выражение, — ветка `Case`, аргумент `Coalesce`, `Greatest` или `Least`,
первый аргумент `NullIf`, — поле проверяет так же, как обычное значение (`ValidationError` для текста длиннее
`max_length`, числа, которого нет среди членов перечисления).

### <a id="count"></a>`count()`

```python
class QuerySet:
    def count(self) -> CountQuery: ...
```

Возвращает `int` — число строк, которое возвращает `await qs`, в том числе с агрегатными вычисляемыми
значениями, фильтром `HAVING`, `.distinct()` или сортировкой через связь «ко многим» (её `JOIN`
возвращает строку на каждую связанную строку). Запрос с `.distinct()`, выбирающий вычисляемое значение
через связь «ко многим» (`F("books__name")`), возвращает строку на каждое различное значение, и `count()`
тоже. `.group_by()` на него не влияет — см. `group_by()`; с `.distinct(<fields>)` он считает строки,
которые выбирает `DISTINCT ON`.

### <a id="aggregate"></a>`aggregate()`

```python
class QuerySet:
    def aggregate(self, **kwargs: Expression | Term) -> AggregateQuery: ...
```

Один словарь с ключом по имени каждого аргумента, по всему запросу (после `.group_by()` — по
сгруппированным строкам, см. `group_by()`). Запрос со срезом
(`qs.order_by("-rating")[:10].aggregate(...)`) агрегирует строки среза, ограниченные по их первичному
ключу; несортированный срез содержит первые строки по первичному ключу — те, что читают
`first()`/`last()`. Срез с `.distinct()` может фильтровать через связь «ко многим»
(`filter(tags__in=[1, 2]).distinct().order_by("id")[:2].aggregate(...)`): его строки сводятся к одной на
первичный ключ до подсчёта, как у `.distinct()` без среза. Любой другой срез, строки которого могут
повторять первичный ключ (фильтр, вычисляемое значение или сортировка через связь «ко многим»), и модель
с составным первичным ключом дают `QueryError` — агрегируйте запрос `.values()`/`.values_list()` со
срезом. Агрегат можно назвать именем поля (`aggregate(price=Sum("price"))`): поле он не заменяет —
фильтры перед ним и другой агрегат того же вызова (`aggregate(s=Sum("i"), c=Count("s"))`) читают поле.

### <a id="in-bulk"></a>`in_bulk()`

```python
class QuerySet:
    async def in_bulk(
        self,
        id_list: Iterable[Any] | None = None,
        *,
        field_name: str = "pk",
    ) -> dict[Any, TModel]: ...
```

Объекты запроса в словаре по их значению поля `field_name` — только те, что хранят значение из `id_list`,
если он передан (пустой даёт `{}` без запроса; длинный список — один связанный параметр — см.
[Длинные списки `__in`](filters.ru.md#large-in-lists)). `field_name` — `"pk"` (ключи составного
первичного ключа — кортежи), поле, объявленное уникальным без условия (у связи — значение её ключа), или
единственное поле `distinct(...)`; любое другое поле даёт `QueryError`, неизвестное — `FieldError`.
`QueryError` на срезанном запросе, запросе значений и объединённом запросе.

### <a id="first-last"></a>`first()` / `last()`

```python
class QuerySet:
    def first(self) -> QuerySetSingle[TRow | None]: ...

    def last(self) -> QuerySetSingle[TRow | None]: ...
```

`LIMIT 1`; без `order_by()` и без `Meta.ordering` `first()` сортирует по первичному ключу по возрастанию
(у запроса с `.group_by()`/`.distinct(<fields>)` остаётся несортированным); `last()` точно обращает
текущую сортировку — и направление, и явное место `NULL` (или сортирует по первичному ключу по убыванию,
если сортировки нет; у модели без первичного ключа и без сортировки последней строки нет, и берётся
любая). На срезе `first()`/`last()` берут из строк среза; несортированный срез для обоих содержит первые
строки по первичному ключу. Строку, взятую из среза (`qs[2:5].first()`, `qs[3]`, а также
`last()`/`latest()`/`earliest()`/`get()` среза), нельзя фильтровать, пересортировать или вызывать у неё
`distinct()` (`QueryError`), как и у самого среза; `first()` запроса без среза — можно. На запросе с
`.distinct(<fields>)` `last()`/`latest()`/`earliest()`/`get()` выбирают среди строк, которые
`DISTINCT ON` возвращает для исходной сортировки.

### <a id="get"></a>`get()`

```python
class QuerySet:
    def get(
        self,
        *args: Q,
        does_not_exist_exception: GetExceptionArgument = GetException.STANDARD,
        multiple_objects_returned_exception: GetExceptionArgument = GetException.STANDARD,
        **kwargs: Any,
    ) -> QuerySetSingle[TRow] | QuerySetSingle[TRow | None]: ...
```

Единственный подходящий объект. `get(**kwargs)` — это `filter(**kwargs).get()`: на запросе с
`.distinct(<fields>)` условия применяются до того, как `DISTINCT ON` выберет строку на группу, как в
`filter()`; на запросе со срезом работает только вызов без аргументов (он читает строки самого среза).
`does_not_exist_exception` — если ничего не найдено — и `multiple_objects_returned_exception` — если
найдено больше одного: по умолчанию бросаются `DoesNotExist` / `MultipleObjectsReturned`; класс
исключения (создаётся без аргументов) или уже созданный объект бросается вместо них; `None` выключает
проверку — без совпадений возвращается `None` (тип результата `Model | None`), при нескольких совпадениях
— одно из них, прочитанное через `LIMIT 1` без подсчёта остальных (какое именно — не определено;
определённое берёт `first()`). Любое другое значение — `TypeError`.

### <a id="create"></a>`create()`

```python
class QuerySet:
    async def create(self, **kwargs: Any) -> TModel: ...
```

Создаёт строку и возвращает её объект — `Model(**kwargs)`, сохранённый на подключении запроса
(`Model.objects.using("replica").create(...)`); фильтры запроса не участвуют. `QueryError` для значения
`F()`/выражения, а также по правилам
[`Meta.tenant_field`](../soft-delete-versions-tenants/multi-tenancy.ru.md), когда переданный арендатор
отличается от активного.

### <a id="get-or-create"></a>`get_or_create()`

```python
class QuerySet:
    async def get_or_create(
        self,
        defaults: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> tuple[TModel, bool]: ...
```

Объект, подходящий под `kwargs` среди строк запроса, либо новый, созданный из `kwargs` и `defaults`, —
`(object, created)`. Проверка существования читает на подключении для записи, поэтому отстающая реплика
не приведёт к дубликату; строка, созданная параллельно между проверкой и вставкой, читается, а не
вызывает ошибку, — её выдаёт конфликт ограничения уникальности при вставке, поэтому на базе без
ограничений уникальности (`Features.supports_unique_constraints`) два параллельных вызова могут вставить
обе строки. `QueryError`, если строка создаётся, а `defaults` противоречит `kwargs`. Строка, которую
запрос не показывает, — мягко удалённая или арендатора вне области, — конфликтующая со вставкой, даёт
`IntegrityError` с этим объяснением (восстановите её или прочитайте через
`include_deleted()`/`all_tenants()`); так же и в `update_or_create()`.

### <a id="update-or-create"></a>`update_or_create()`

```python
class QuerySet:
    async def update_or_create(
        self,
        defaults: dict[str, Any] | None = None,
        create_defaults: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> tuple[TModel, bool]: ...
```

Обновляет объект, подходящий под `kwargs`, значениями `defaults` либо создаёт его с `create_defaults` (с
`defaults`, если они не заданы) — `(object, created)`. Выполняется в транзакции и блокирует найденную
строку (`SELECT ... FOR UPDATE`), если база это умеет. Как и в `get_or_create()`, строку, созданную
одновременно, отличает конфликт уникальности при вставке, поэтому в базе без ограничений уникальности
(`Features.supports_unique_constraints` — ClickHouse) два одновременных вызова могут вставить обе строки.
`QueryError`, если `defaults` задаёт `Meta.optimistic_lock_field`, `FieldError` для неизвестного поля.

### <a id="bulk-create"></a>`bulk_create()`

```python
class QuerySet:
    def bulk_create(
        self,
        objects: Iterable[TModel],
        batch_size: int | None = None,
        *,
        ignore_conflicts: bool = False,
        update_fields: Iterable[str] | None = None,
        on_conflict: Iterable[str] | None = None,
        on_conflict_constraint: str | None = None,
        conflict_where: Q | RawSQLTerm | None = None,
        returning: bool | None = None,
        use_copy: bool = False,
    ) -> BulkCreateQuery[TModel]: ...
```

Возвращает объекты в том виде, в каком их передали (и объекты генератора) — вместе с теми, что пропущены или
обновлены из-за конфликта. Сгенерированные первичные ключи, колонки `GeneratedField` и пропущенные колонки с
`db_default` **не** записываются обратно в объекты, если не передан `returning=True` (через `RETURNING`;
SQLite не сохраняет порядок вставки в многострочном `RETURNING`, поэтому там у каждого объекта должен быть
задан pk — строки сопоставляются по нему, а при `on_conflict` — по его полям: объект, обновивший сохранённую
строку, получает её ключ, — а объект без pk даёт `UnSupportedError`; на базе без `RETURNING` — ClickHouse —
`returning=True` даёт `UnSupportedError`, а ключ, который hare берёт до вставки (серия ключей ClickHouse),
оказывается в объектах и без него). Без него `GeneratedField` записанного объекта остаётся непрочитанным — его
чтение даёт `NoValuesFetched`, пока его не прочитает `refresh_from_db()`. При `None` (по умолчанию) берётся
`Meta.returning` (по умолчанию `False`) — задайте его на общей базовой модели, чтобы не передавать
`returning=True` в каждом вызове; явные `True`/`False` здесь всегда важнее, а значение, **унаследованное** от
`Meta.returning`, вместе с `use_copy=True` или на SQLite для объекта без pk молча уступает, а не вызывает
ошибку. `use_copy=True` загружает строки через протокол `COPY` PostgreSQL вместо `INSERT` с параметрами —
быстрее для больших партий, но несовместимо с явным `returning=True` и со всеми аргументами обработки
конфликтов (у `COPY` нет `RETURNING`/`ON CONFLICT`) и не поддерживается в SQLite. На ClickHouse
`bulk_create()` загружает строки одной двоичной вставкой и с `use_copy=True`, и без него — см. [Массовая
загрузка](../dialects/clickhouse/differences.ru.md#bulk-loads). `COPY` загружает только колонки целых, чисел с
плавающей точкой, numeric, логических, текстовых, UUID, даты, времени, timestamp, JSON и двоичных типов —
модель с любой другой колонкой (массив, диапазон или мультидиапазон, интервал, сетевой адрес, нативный enum,
тип расширения вроде `CitextField`, `HStoreField`, `LtreeField`, `PostGISField`, `TSVectorField`) даёт
`UnSupportedError` с именем поля на любом драйвере, проверяется одинаково на стороне Python до обращения к
драйверу; для такой модели используйте `use_copy=False`. `update_fields`/`on_conflict` принимают имена полей:
колонка `source_field` определяется сама, имя прямой связи означает колонки её ключа (`owner` → `owner_id`),
неизвестное имя даёт `FieldError`; генерируемое поле, `Meta.tenant_field`, `Meta.optimistic_lock_field` или
`Meta.soft_delete_field` в `update_fields` дают `QueryError`. У конфликтующей строки обновляются и поля
`auto_now` (и увеличивается `Meta.optimistic_lock_field`); конфликтующая мягко удалённая строка обновляется,
но остаётся удалённой. Поле из `update_fields`, значение которого у всех объектов оставлено на `db_default`,
всё равно обновляется — значением по умолчанию из базы. С `returning=True` объект с явно заданным первичным
ключом тоже получает свои колонки `GeneratedField`/пропущенные `db_default`, вставка-или-обновление модели с
`Meta.optimistic_lock_field` читает увеличенную версию строки, а при `ignore_conflicts=True` из нескольких
объектов с одним ключом конфликта в одном вызове сохранённым остаётся только первый (тот, чья строка
вставлена); объект, который пропустил конфликт, сохраняет данный ему pk — `None` для генерируемого, — ведь
строки для него не записано. С `update_fields` несколько объектов с одним ключом конфликта в одном вызове
записываются по порядку на любом драйвере, поэтому побеждают значения последнего; с `returning=True` каждый из
них получает первичный ключ строки. Каждый объект должен быть объектом именно этой модели (другая модель,
модель с той же абстрактной базой или подкласс со своей таблицей дают `QueryError`), а объект, загруженный
через `.only()`/`.defer()`, должен содержать все колонки, которые записывает вставка
(`IncompleteInstanceError`). Собственные дополнительные параметры команды (область арендатора и увеличение
версии при вставке-или-обновлении) учитываются в ограничении на число параметров при выборе размера партии.
Модель, все колонки которой берутся из значений по умолчанию базы, записывается одним `INSERT ... SELECT FROM
generate_series(...)` в PostgreSQL (одним `DEFAULT VALUES` на объект в SQLite). Если `bulk_create()`
завершился ошибкой, объекты остаются такими, какими были до вызова (первичный ключ, возвращённые колонки,
признак сохранённости), даже если более ранние партии уже были записаны и затем откачены. С `use_copy=True`
несколько команд `COPY` (объекты с явным первичным ключом и без него загружаются отдельно, как и партии
`batch_size`) тоже выполняются в одной транзакции — на обоих драйверах PostgreSQL; внутри
`Transactions.atomic()` каждая `COPY` выполняется на соединении транзакции и фиксируется или откатывается
вместе с ней, в том числе при откате точки сохранения. `on_conflict_constraint` не поддерживается в SQLite;
`on_conflict_constraint="pk"` называет первичный ключ на любой базе — как `on_conflict` по его полям, какое бы
имя ни было у его ограничения. `conflict_where` (только PostgreSQL) — `Q` по полям модели или `RawSQLTerm`
готового SQL — повторяет условие `WHERE` частичного уникального индекса, чтобы `ON CONFLICT (...) WHERE
<conflict_where>` мог на него указать; несовместимо с `on_conflict_constraint` и требует, чтобы `on_conflict`
называл колонки этого индекса.

<a id="bulk-create-ignore-conflicts"></a>**`ignore_conflicts=True`** возвращает вставленные объекты в
переданном порядке: пропущенные из-за конфликта в результат не входят, сохраняют данный им pk и не
помечаются сохранёнными. Операторы читают свои строки обратно (`RETURNING`, с `returning=True` и без), и
каждая строка сопоставляется со своим объектом по полям `on_conflict`, иначе — по первичному ключу, по
значениям в том виде, в каком их записывают поля (домен `EmailField` в нижнем регистре); где база
возвращает строки в произвольном порядке (SQLite) — по всем записанным колонкам, поэтому различаются и
объекты без значения цели конфликта. Объектам, ключи которых генерирует база,
нужен `on_conflict=[...]` — без него вызов падает с
`UnSupportedError` до любого SQL, как и на базе без `RETURNING`. Из нескольких объектов с одним ключом
конфликта в вызове вставляется первый.

```python
inserted = await Subscriber.objects.bulk_create(subscribers, ignore_conflicts=True, on_conflict=["email"])
await notify_new_subscribers(inserted)   # без второго запроса за записанными строками
```

На базе без `INSERT ... ON CONFLICT` (ClickHouse, `Features.supports_insert_conflicts` выключен)
`bulk_create()` с `ignore_conflicts`, `update_fields`, `on_conflict` или `on_conflict_constraint` даёт
`UnSupportedError` до отправки SQL.

### <a id="bulk-update"></a>`bulk_update()`

```python
class QuerySet:
    def bulk_update(
        self,
        objects: Iterable[TModel],
        fields: Iterable[str],
        batch_size: int | None = None,
        *,
        returning: bool | None = None,
    ) -> BulkUpdateQuery[TModel]: ...
```

Не поддерживает поля «многие-ко-многим» и обратных связей (`FieldError`) и значения `F()`/выражения
(`QueryError` — используйте `.update()` или `save()`); требует первичный ключ у каждого объекта. Объект,
не загрузивший поле `auto_now` (`.only()`/`.defer()`), получает отметку всё равно. `returning=True`
добавляет `RETURNING`, возвращающий свежее, пересчитанное базой значение каждой колонки `GeneratedField`
в каждый успешно обновлённый объект; строки сопоставляются по первичному ключу, а не по позиции, поэтому
(в отличие от `bulk_create()`) это работает на любом диалекте, а не только в PostgreSQL. На модели без
`GeneratedField` ничего не делает. При `None` берётся `Meta.returning`, как у `bulk_create()`. На модели
с `Meta.optimistic_lock_field` версия в памяти увеличивается у каждого объекта, строка которого
действительно записалась (а при `track_dirty_fields` сбрасывается его снимок изменений), и только потом
даётся `StaleObjectError` для строк партии, которые не записались. Объект, строку которого исключает
собственный `.filter(...)` запроса, просто не обновляется и не считается устаревшим (срез запроса и
запрос с `.distinct(<fields>)` дают `QueryError`, как у `.update()`). В отличие от
обычного `.update()`, здесь устаревание проверяется: у каждого объекта есть ранее загруженная версия для
сравнения. `fields` — непустой список имён полей (строка вместо списка или пустой список дают
`QueryError`); повторённое имя записывается один раз, неизвестное даёт `FieldError`, а первичный ключ
(`"pk"` или его поле) даёт `QueryError` — по нему сопоставляются строки. Каждый объект должен быть
объектом именно этой модели (иначе `QueryError`), а объект, загруженный через `.only()`/`.defer()`,
должен содержать первичный ключ и все обновляемые поля (`IncompleteInstanceError`). Размер партии
оставляет место для собственного фильтра запроса (включая область арендатора) и параметров CTE. Если
объекты разбиты на несколько команд только ради ограничения на число параметров (`batch_size` не задан),
все они выполняются в одной транзакции — ошибка в любой (например, `IntegrityError`) не оставляет ни
одной обновлённой строки; устаревшая версия такой ошибкой не считается и не откатывает другие строки —
она сообщается после выполнения всех команд. С явным `batch_size` каждая партия — своя команда без общей
транзакции: ошибка в более поздней партии оставляет более ранние зафиксированными, а устаревший объект в
одной партии не мешает записи остальных. Модель с `Meta.change_capture` пишет все партии в одной
транзакции со своими событиями — см. [Захват изменений](../integrations/outbox.ru.md#change-capture).
На базе, которая не сообщает числа изменённых записью строк (ClickHouse), возвращает `None` и считает
записанным каждый объект: устаревших объектов не находит, а версию, снимок изменённых полей и значения
`auto_now` применяет к каждому.

### <a id="only"></a>`only()`

```python
class QuerySet:
    def only(self, *fields_for_select: str) -> Self: ...
```

Загружает только перечисленные поля — получается частично загруженная модель. `QueryError`, если список
пуст или вместе с `.defer()`. Чтение незагруженного поля даёт `NoValuesFetched` (это `AttributeError`,
поэтому `hasattr()` — `False`) с его именем — и `pk`, если не загружен первичный ключ; чтобы сохранить
или обновить объект, укажите его. Запись `relation__field` загружает и первичный ключ каждой связанной
модели по пути, поэтому внешний ключ `NULL` даёт `None`, а существующая связанная строка всегда даёт
объект.

### <a id="select-related"></a>`select_related()`

```python
class QuerySet:
    def select_related(self, *args: str | Select) -> Self: ...
```

Загружает связанные объекты в том же запросе через `JOIN`; принимает строку или
`Select(relation, extra_condition=Q(...))`. Каждый шаг пути должен быть связью с одним значением —
`ForeignKeyField`, `OneToOneField` или обратным `OneToOneField`; обратный `ForeignKeyField` или
`ManyToManyField` (для них — `prefetch_related()`), обычное поле или неизвестное имя сразу дают
`FieldError`. Условие `Select` входит в `JOIN` связи везде, где запрос её пересекает: фильтр, сортировка
или `.values()` через неё читают только связанные строки, подходящие под условие, — как и `count()`,
`exists()`, `aggregate()`, `update()` и `delete()` этого QuerySet, которые выбирают те же строки, что и
он сам.

### <a id="prefetch-related"></a>`prefetch_related()`

```python
class QuerySet:
    def prefetch_related(self, *args: str | Prefetch | None) -> Self: ...
```

Загружает связанные объекты отдельными запросами (не через `JOIN` с выбираемыми строками); принимает
строку (в том числе `"a__b"`) или `Prefetch(...)`. `FieldError` для неизвестного поля или поля, которое
не является связью. Строки связи «многие-ко-многим» приходят одним запросом, соединённым со связующей
таблицей, когда связующая таблица и связанные строки лежат на одном соединении, у связи нет модели
`through=`, а QuerySet из `Prefetch` не фильтрует по этой связи, не добавляет вычисляемых значений,
`.distinct()` и вложенной предзагрузки; иначе сначала отдельным запросом читается связующая таблица.

`count()`, `exists()` и `aggregate()` у QuerySet с предзагрузкой дают `QueryError` — они не возвращают
объектов, к которым прикрепить связи; `prefetch_related(None)` снимает всю предзагрузку, заданную раньше:
`books.prefetch_related(None).count()`. Так считает свои строки запрос из параметров.

### <a id="include-deleted-only-deleted"></a>`include_deleted()` / `only_deleted()`

```python
class QuerySet:
    def include_deleted(self) -> Self: ...

    def only_deleted(self) -> Self: ...
```

Мягко удалённые строки вместе с живыми / только мягко удалённые — вместо фильтра
[`Meta.soft_delete_field`](../soft-delete-versions-tenants/soft-delete.ru.md) по умолчанию. Действует
последний из двух вызовов; связи, до которых доходит запрос, тоже видят удалённые строки. `QueryError`
для модели без `Meta.soft_delete_field`.

### <a id="get-connection"></a>`get_connection()`

```python
class QuerySet:
    def get_connection(self, *, for_write: bool = False) -> DatabaseClient: ...
```

Подключение, на котором запрос выполнился бы сейчас: закреплённое через `using()`, иначе выбор
маршрутизатора (с арендатором, для которого построен запрос), иначе подключение объекта, от которого
получен связанный менеджер, иначе подключение модели по умолчанию; внутри открытой на нём транзакции —
клиент транзакции. Ничего не закрепляет — пример с блокировкой см. в
[`Model.get_connection()`](../models/model-methods.ru.md#get-connection).

### <a id="iterator"></a>`iterator()`

```python
class QuerySet:
    def iterator(self, chunk_size: int = 1000) -> AsyncIterator[TRow]: ...
```

Без `.order_by()` страницы идут по `Meta.ordering`, иначе по первичному ключу (по всем частям
составного); к сортировке, в которой возможны равные значения (нет первичного ключа, нет уникального поля
без `NULL` и нет `UniqueConstraint` по полям без `NULL`), добавляется первичный ключ, чтобы между
страницами ни одна строка не пропала и не повторилась — строки с равными значениями сортировки идут по
первичному ключу. Колонка сортировки, которую `.only()`/`.defer()` не загружают, всё равно читается для
курсора страницы и остаётся незагруженной в выдаваемых объектах. Использует постраничный вывод по курсору
(безопасен при одновременных удалениях), когда каждое поле `order_by()` — обычная колонка этой модели;
иначе переходит на `LIMIT`/`OFFSET` (НЕ безопасен при одновременных удалениях: удалённая уже выданная
строка сдвигает следующие, и одна пропускается), если в сортировке есть поле связанной модели или
вычисляемое значение, либо вычисляемое значение — оконная функция (условие по курсору изменило бы строки,
по которым она считается). Учитывает собственные `.limit()`/`.offset()`/срез запроса и `.after_cursor()`.
Запрос, который может вернуть один первичный ключ несколько раз — фильтр или вычисляемое значение (`F()`,
функция, `Case`, арифметика) через связь «ко многим» или `.distinct()`, выбирающий такое вычисляемое
значение, — идёт через `OFFSET` с сортировкой по этим вычисляемым значениям после первичного ключа, чтобы
ни одна повторённая строка не пропала. Запрос с `.distinct(<fields>)` перебирает строки, которые выбирает
`DISTINCT ON`. Страница запрашивается с одной строкой сверх `chunk_size`: по ней видно, есть ли следующая
страница, и результат, который кончается на границе страницы, не требует пустого запроса после неё.

### <a id="dates"></a>`dates()`

Различающиеся значения `DateField`/`DatetimeField`, усечённые до `trunc_type` (`year`, `quarter`,
`month`, `week` — с понедельника — или `day`), как `datetime.date`; у даты-времени — в текущем поясе;
`NULL` не попадают; в порядке `order` (`ASC`/`DESC`); QuerySet `values_list(flat=True)`, его можно
фильтровать и срезать как обычно.

## <a id="null-ordering"></a>Порядок NULL при сортировке

SQLite и PostgreSQL по-разному решают, где окажется `NULL`, а простая строка сортировки оставляет это
диалекту: SQLite считает `NULL` самым малым значением (первым при `ASC`, последним при `DESC`),
PostgreSQL — самым большим (последним при `ASC`, первым при `DESC`). Если в `Book(title, rating)` лежат
`a:1, b:NULL, c:2, d:3, e:NULL`, то `order_by("rating", "title")` вернёт `b, e, a, c, d` в SQLite и
`a, c, d, b, e` в PostgreSQL. ClickHouse сортирует так же, как PostgreSQL. Это поведение по умолчанию
действует для `order_by("field")`/`order_by("-field")`, строк `Meta.ordering`,
`first()`/`last()` и строк в `Window(order_by=[...])`.

Чтобы место `NULL` было одинаковым на любом диалекте, передайте вместо строки `Ordering`, построенный из
`F(...)`:

```python
from hare.query.expressions import F

await Book.objects.all().order_by(F("rating").desc(nulls_last=True), "title")   # 3, 2, 1, затем NULL
await Book.objects.all().order_by(F("rating").asc(nulls_first=True), "title")   # сначала NULL, затем 1, 2, 3
await Book.objects.all().order_by(F("editor__name").asc(nulls_last=True))  # в том числе NULL из LEFT JOIN
```

`F(name).asc()` / `F(name).desc()` принимают только именованные флаги `nulls_first` / `nulls_last` (оба
`True` дают `QueryError`; ни одного — оставляют поведение диалекта). `Ordering` и строки можно свободно
смешивать, и `Ordering` принимается везде, где принимается сортировка: `order_by()` (и у `union()`),
`latest()`/`earliest()`, `Meta.ordering`, `Window(order_by=[...])`, `distinct(...)` вместе с `order_by()`
(PostgreSQL), `values()`/`values_list()`, `stream()`/`iterator()`. Всё, что не строка и не `Ordering`,
даёт `QueryError`. В SQL получается `ORDER BY ... NULLS FIRST|LAST` на любой базе.

- **`latest()` / `earliest()` не выбирают `NULL`.** Для простого имени поля на любом диалекте добавляется
  `NULLS LAST`, поэтому `NULL` никогда не побеждает: `latest("rating")` — строка с наибольшим значением,
  не равным `NULL`, а `earliest("rating")` — с наименьшим. Если все значения `NULL`, строка с `NULL` всё
  равно возвращается (а не `None`). Колонка, которая не может хранить `NULL`, сохраняет простое
  направление, чтобы по-прежнему работал просмотр по индексу; сортировка `related__field` и вычисляемое
  значение считаются допускающими `NULL`. Явный `Ordering` соблюдается: `earliest()` использует его как
  задано, `latest()` обращает его, как `last()`.
- **`last()` обращает сортировку точно.** Обращаются и направление, и явное место `NULL`
  (`ASC NULLS LAST` становится `DESC NULLS FIRST`), поэтому `last()` — всегда последняя строка той же
  сортировки при чтении с начала. Без явного места `NULL` поведение диалекта обращается вместе с
  направлением.
- **`after_cursor()` / `before_cursor()` / `iterator()`** следуют месту `NULL` в самой сортировке: явный
  `nulls_first`/`nulls_last` определяет, какие строки лежат после (или до) курсора (включая границу со
  значением `NULL`), иначе это определяет диалект, поэтому страницы ни в одном случае не теряют и не
  повторяют строк. `before_cursor()` при обращении сортировки отражает и место `NULL` (`ASC NULLS FIRST`
  выполняется как `DESC NULLS LAST`).
- **Планы команд** учитывают место `NULL`, поэтому два запроса, отличающиеся только им, никогда не делят
  один план.

## <a id="prefetch"></a>`Prefetch`

```python
class Prefetch:
    def __init__(
        self,
        relation: str,
        queryset: QuerySet[Any],
        to_attribute: str | None = None,
    ) -> None: ...
```

К `queryset` уже могут быть применены `.filter()`/`.only()`/`.order_by()`; это должен быть запрос к
модели — запрос `.values()`/`.values_list()` даёт `QueryError`. `to_attribute` не может совпадать с
именем поля, связи, колонки ключа (`author_id`) или другого атрибута модели, которой принадлежит
загружаемая связь, — это даёт `QueryError`, а не перезаписывает атрибут в каждом объекте. Вложенная
предзагрузка поддерживается двумя способами:

```python
# путь через связи внутри Prefetch
await Author.objects.all().prefetch_related(Prefetch("books__editor", queryset=Reader.objects.filter(is_active=True)))

# .prefetch_related() прямо у запроса, переданного в Prefetch
await Author.objects.all().prefetch_related(
    Prefetch("books", queryset=Book.objects.filter(in_print=True).prefetch_related("editor"))
)
```

Путь через связь (`"books__editor"`) загружает собственный атрибут связи (`author.books`), даже если
рядом передан `Prefetch` той же связи с `to_attribute=` — тот загружает только свой атрибут. Путь
попадает в результаты `to_attribute`, когда начинается с имени `to_attribute`; задающий его `Prefetch`
может стоять дальше в том же вызове или в более раннем вызове `prefetch_related()`:

```python
authors = await Author.objects.all().prefetch_related(
    Prefetch("books", queryset=Book.objects.filter(in_print=True), to_attribute="books_in_print"),
    "books_in_print__editor",  # editor у каждой книги из author.books_in_print
    "books",  # author.books — все книги автора
)
```

Первое имя, которое не является ни связью модели, ни таким `to_attribute`, даёт `FieldError`, как и
путь с пустой частью (`"books__"`). Два `Prefetch`, которые задают один атрибут — одинаковый
`to_attribute` или имя самой связи — с разными запросами, дают `QueryError`: поздние объекты заменили
бы ранние; один и тот же `Prefetch`, переданный дважды, загружается один раз.

### <a id="sliced-prefetch"></a>Срез у запроса `Prefetch`

Срез у запроса `Prefetch` (`[:3]`, `[2:5]`, `.limit()`, `.offset()`) берётся из строк **каждого
родителя** — три новых отзыва к каждой книге, а не три отзыва всего:

```python
books = await Book.objects.prefetch_related(
    Prefetch("reviews", queryset=Review.objects.order_by("-id")[:3], to_attribute="latest_reviews")
)
```

Строки нумеруются внутри каждого родителя через
`ROW_NUMBER() OVER (PARTITION BY <the parent's key> ORDER BY <the queryset's ordering>)`, и номер
сравнивается с границами среза — один запрос для обратного внешнего ключа; для связи «многие-ко-многим»,
где строка может принадлежать нескольким родителям и попадать в срез каждого отдельно, сначала читаются
пронумерованные пары (родитель, строка), затем сами строки. Сортировка — `order_by()` запроса, иначе
`Meta.ordering` модели, иначе первичный ключ. Составные ключи, `to_attribute` и вложенная предзагрузка
строк среза работают так же, как без среза. Аннотация, читающая `Window(...)`, считается по строкам всех
родителей, как без среза: для обратного внешнего ключа и связи «многие ко многим» эти строки читаются, а
срез каждого родителя берётся из них. У прямой связи и обратной «один-к-одному» у родителя не больше
одной строки: срез, начинающийся с неё, её оставляет, срез после неё даёт `None`.

## <a id="stream"></a>Курсор на стороне сервера: `stream()`

```python
class QuerySet(QuerySpecification):
    def stream(self, chunk_size: int = 1000) -> AsyncIterator[TRow]: ...
```

В отличие от `iterator()` (который для каждой страницы выполняет новый `SELECT` через `LIMIT`/`OFFSET`
или по курсору), `stream()` открывает ровно **один** курсор на стороне сервера на весь проход — один
согласованный снимок данных на весь перебор. Строка, уже попавшая в поток курсора, не зависит от
одновременного `DELETE`/`UPDATE` посреди перебора, а ещё не прочитанные строки читаются по снимку,
сделанному при открытии курсора, а не по тому, как таблица выглядит в момент чтения каждой строки.

```python
async with Transactions.atomic():
    async for book in Book.objects.filter(in_print=False).stream():
        ...
```

Требует активного блока `Transactions.atomic()` на обоих драйверах PostgreSQL — серверный курсор живёт в
своей транзакции: `Connection.cursor()` в `asyncpg`, `DECLARE ... NO SCROLL CURSOR` в `rust_pg`, — и в
SQLite, где курсор читается на соединении транзакции: по `chunk_size` строк за обращение
`fetchmany()` к рабочему потоку aiosqlite, весь результат в памяти никогда не держится, а курсор
закрывается, когда перебор закончен, прерван или отменён. Вне транзакции даёт `QueryError`, а на базе без
`features.supports_streaming` (без транзакций) — `UnSupportedError`. `chunk_size` — сколько строк
забирать за одно обращение; его учитывают `Connection.cursor(prefetch=...)` в `asyncpg`,
`FETCH FORWARD <chunk_size>` курсора `rust_pg` и партии SQLite. Пока поток открыт, в той же
транзакции можно выполнять другие запросы — из тела цикла или из соседних задач (`asyncio.gather()`),
разделяющих транзакцию; каждое чтение курсора ждёт своей очереди на соединении транзакции.

В ClickHouse (`features.streams_without_transaction`) `stream()` работает и вне транзакции: сервер отдаёт
строки по мере вычисления блоками, которые драйверы читают по одному, а выход из цикла — `break`, ошибка,
отмена — закрывает поток и возвращает соединение.

Если перебор заканчивается раньше — исключение, отмена или `aclose()` генератора (простой `break`
закрывает его, только когда генератор соберёт сборщик мусора; оберните его в `contextlib.aclosing()`,
чтобы закрыть на `break`), — поток сразу останавливается, не читая оставшиеся строки; у `rust_pg`
следующая порция не запрашивается, а курсор заканчивается вместе с транзакцией.
`COMMIT`/`ROLLBACK` самой транзакции тоже закрывают ещё открытый на ней поток; чтение из него после этого
даёт `TransactionManagementError`. У `rust_pg` `ROLLBACK`, на который нет ответа в течение 30 с, вместо
этого закрывает соединение — сервер всё равно откатывает транзакцию, а соединение не возвращается в пул.

## <a id="recursive"></a>Рекурсивные запросы и CTE

### <a id="with_recursive-walking-a-relation-of-a-model-to-itself"></a>Обход связи модели с самой собой: `with_recursive()`

```python
class QuerySet(QuerySpecification):
    def with_recursive(
        self,
        relation: str,
        *,
        max_depth: int | None = None,
    ) -> QuerySet[TModel, TModel]: ...
```

`with_recursive()` берёт строки запроса как начало и идёт от них по связи `relation` снова и снова:
строки на шаг дальше, потом строки на шаг дальше от них и так далее. Результат — запрос строк модели, до
которых дошёл обход, вместе с начальными строками; он строится одним запросом `WITH RECURSIVE`, без
обращения к базе на каждый уровень.

```python
class Category(Model):
    name = fields.CharField(max_length=50)
    parent = fields.ForeignKeyField("models.Category", related_name="children", null=True)
    see_also = fields.ManyToManyField("models.Category", related_name="seen_from")

fiction = await Category.objects.get(name="Fiction")
# Fiction и все категории ниже неё: обратная связь, вниз по дереву.
everything = await Category.objects.filter(pk=fiction.pk).with_recursive("children")
# Категория и её родители до самого верха: прямая связь, вверх по дереву.
chain = await Category.objects.filter(name="Space opera").with_recursive("parent")
# Только два уровня вниз — дочерние категории и их дочерние.
nearby = await Category.objects.filter(pk=fiction.pk).with_recursive("children", max_depth=2)
# Все категории, до которых «Space opera» доходит по графу «многие ко многим».
reached = await Category.objects.filter(name="Space opera").with_recursive("see_also")
```

- **`relation`** — прямая (`ForeignKeyField`, `OneToOneField`), обратная или «многие-ко-многим» связь,
  связанная модель которой — сама модель. Всё остальное — обычное поле, связь с другой моделью, путь
  через `__` — даёт `FieldError` при выполнении запроса.
- **`max_depth`** — сколько шагов от начальных строк: `0` — только они, `1` — ещё их прямые соседи,
  `None` (по умолчанию) — обход идёт, пока появляются новые строки. `int` от `0` до `100_000`; другое
  значение (отрицательное или большее число, `True`, `2.0`, строка) — `QueryError` при вызове.
- **Циклы заканчиваются.** Шаги обхода объединяются через `UNION`, поэтому строка, достигнутая второй раз
  — по циклу в данных (`a.parent = b`, `b.parent = a`) или двумя путями, — не добавляет новой строки, и
  обход останавливается. С `max_depth` строка, достигнутая на двух глубинах, остаётся одна. ClickHouse
  соединяет шаги через `UNION ALL`, поэтому там цикл заканчивает только `max_depth` — без него обход даёт
  `UnSupportedError`.
- **Начальные строки** — любой запрос: фильтр, несколько строк, срез, запрос с сортировкой или
  вычисляемыми значениями (читаются только первичные ключи). Пустое начало — пустой результат.
- **Видимость строк.** Обход идёт по строкам, которые показывает область видимости модели по умолчанию:
  мягко удалённая строка (`Meta.soft_delete_field`) или строка другого арендатора (`Meta.tenant_field`)
  не возвращается и не проходится, поэтому строки под удалённой строкой сверху не достигаются.
  `include_deleted()` / `all_tenants()` до `with_recursive()` проводят обход через все строки, и
  результат сохраняет эту видимость.
- **Составные первичные ключи** работают так же, как ключи из одной колонки: обход сравнивает все колонки
  ключа.
- **Результат** — обычный запрос модели на том же подключении: его можно фильтровать, сортировать, брать
  срез, `count()`, читать `values()`, предзагружать связи или передавать значением `__in`
  (`Category.objects.exclude(pk__in=everything)`). Порядок по умолчанию — порядок модели, порядок обхода
  не сохраняется; глубина не входит в колонки строк.
- Объединённый запрос (`union()`/`intersection()`/`difference()`) даёт `QueryError` — вызывайте
  `with_recursive()` у ветки.

Запрос обходит первичные ключи строк в CTE и читает по ним строки модели:

```sql
SELECT ... FROM "category" WHERE "id" IN (
  WITH RECURSIVE "hare_recursive_rows"("id") AS (
    SELECT "hare_recursive_start"."id" FROM "category" "hare_recursive_start"
    WHERE "hare_recursive_start"."id" IN (SELECT "id" FROM "category" WHERE "id" = $1)
    UNION
    SELECT "hare_recursive_previous__children"."id" FROM "hare_recursive_rows"
    JOIN "category" "hare_recursive_previous" ON "hare_recursive_rows"."id" = "hare_recursive_previous"."id"
    JOIN "category" "hare_recursive_previous__children"
      ON "hare_recursive_previous"."id" = "hare_recursive_previous__children"."parent_id"
  )
  SELECT "id" FROM "hare_recursive_rows"
)
```

Для его запроса [план](query-plan-cache.ru.md) не хранится: шаги соединяют связанные строки с условием их
видимости, а такие значения план привязать не может.

### <a id="cterows-reading-a-querysets-rows-from-a-cte"></a>Строки запроса из CTE: `CteRows`

`with_cte(name, query)` добавляет к запросу `WITH`, не меняя его `FROM`. `CteRows(name, *columns)` —
значение, которым фильтр `__in` читает строки CTE, — `SELECT <columns> FROM <name>`:

```python
from hare.query.expressions import CteRows

# Запрос как тело CTE.
long_books = Book.objects.filter(pages__gte=400).values("id")
rows = await Book.objects.with_cte("long_books", long_books).filter(id__in=CteRows("long_books", "id"))

# Рекурсивный CTE, собранный вручную: категория и её предки.
connection = Category.get_connection()
query_class = connection.query_class
category, ancestors = Table("category"), Table("ancestors")
columns = (category.id, category.parent_id)
base = query_class.from_(category).select(*columns).where(category.id == leaf_id)
step = query_class.from_(category).join(ancestors).on(category.id == ancestors.parent_id).select(*columns)
union_all = base * step
union_all.base_query.wrap_set_operation_queries = False
path = await Category.objects.with_cte("ancestors", union_all).filter(id__in=CteRows("ancestors", "id"))

# Составной первичный ключ: колонки CTE в порядке ключа.
picked = Shelf.objects.filter(number=1).values("store", "number")
rows = await Shelf.objects.with_cte("picked", picked).filter(pk__in=CteRows("picked", "store", "number"))
```

`CteRows` требует имя и хотя бы одну колонку — непустые строки, иначе `QueryError`. Составной `pk__in`
сравнивает столько колонок, сколько их в ключе, в порядке ключа. В отличие от `RawSQL`, `CteRows`
экранирует имена так, как принято в базе, и сохраняет [план](query-plan-cache.ru.md) запроса.

## <a id="sample"></a>Выборка из таблицы

```python
class QuerySet(QuerySpecification):
    def sample(
        self,
        percent: float,
        *,
        method: TableSampleMethod | str = TableSampleMethod.BERNOULLI,
        seed: int | None = None,
    ) -> Self: ...
```

`sample()` читает случайную выборку таблицы модели вместо всей таблицы — `TABLESAMPLE` во `FROM`. Сначала
из таблицы берётся выборка, а уже к её строкам применяются фильтры, соединения, группировка и сортировка
запроса. Это дешёвый способ оценить что-то по большой таблице: количество, среднее, несколько строк для
просмотра.

```python
from hare.query.enums import TableSampleMethod

# Около 1% строк — каждая строка остаётся с вероятностью 1%.
rough = await Loan.objects.sample(1).count() * 100
# Одни и те же строки при каждом запросе, пока таблица не меняется.
preview = await Loan.objects.sample(5, seed=42).filter(returned_at__isnull=True).order_by("id")[:20]
# Быстрее на огромной таблице: остаются или пропускаются целые блоки хранения.
average = await Loan.objects.sample(0.5, method=TableSampleMethod.SYSTEM).aggregate(avg=Avg("fee"))
```

```sql
SELECT ... FROM "loan" TABLESAMPLE BERNOULLI (5) REPEATABLE (42) WHERE "returned_at" IS NULL ORDER BY "id" LIMIT 20
```

- **`percent`** — вероятность, с которой остаётся каждая строка (или блок), `int` или `float` от `0` до
  `100`: `100` читает все строки, `0` — ни одной. Число прочитанных строк случайно и близко к `percent`
  от таблицы, но не равно ему точно.
- **`method`** — `TableSampleMethod.BERNOULLI` (по умолчанию) читает всю таблицу и оставляет каждую
  строку отдельно; `TableSampleMethod.SYSTEM` оставляет или пропускает целые блоки хранения — намного
  быстрее на большой таблице, но строки одного блока приходят вместе, поэтому выборка менее равномерна.
  Имя принимается и строкой в любом регистре (`"system"`).
- **`seed`** — `int` от `0` до `2_147_483_647` (`REPEATABLE`): одно и то же значение читает одни и те же
  строки, пока таблица не меняется; `None` (по умолчанию) — каждый раз новая выборка.
- Неверный аргумент — отрицательный или больший процент, `True`, `"10"`, `nan`, другой метод,
  отрицательное значение `seed`, `1.0` как `seed` — даёт `QueryError` при вызове.
- **Где работает.** Строки модели, `values()`/`values_list()`, `count()`, `exists()`, `aggregate()`,
  группы `group_by()`/`annotate()`, `select_related()` (выборка — из таблицы модели, связанные строки
  присоединяются к ней полностью) и запрос как значение подзапроса
  (`filter(pk__in=Loan.objects.sample(10))`).
- **Запись.** `update()`, `delete()` и `hard_delete()` у выборки дают `QueryError`: `TABLESAMPLE` есть
  только в `SELECT`. Записывайте выбранные строки через их ключи:
  `Loan.objects.filter(pk__in=Loan.objects.sample(10, seed=1)).delete()`.
- `Meta.manager`, у которого `get_queryset()` возвращает выборку, даёт `ConfigurationError`, когда связи
  нужна его область видимости как условие `JOIN`, — как и `with_cte()`. Объединённый запрос
  (`union()`/...) даёт `QueryError` — берите выборку у ветки.
- PostgreSQL и ClickHouse (`features.supports_table_sample`): на другой базе запрос даёт
  `UnSupportedError` до отправки SQL. Для его запроса [план](query-plan-cache.ru.md) не хранится: процент
  и `seed` записаны во `FROM`.
- ClickHouse делает выборку по ключу выборки таблицы — `SAMPLE <share>`, без метода и `seed`: см.
  [его выборку](../dialects/clickhouse/query-modifiers.ru.md#sample).

## <a id="returning"></a>Записанные строки

`update(...)`, `delete()` и `hard_delete()` возвращают число записанных строк (`None` на базе, которая его
не сообщает: ClickHouse). `.returning()` у них
возвращает сами строки — записанные один раз и прочитанные той же командой:

```python
# Названные поля каждой обновлённой строки в записанном виде — словарь на строку.
rows = await Book.objects.filter(copies__lt=5).update(price=F("price") * 1.1).returning("id", "price")
# [{"id": 3, "price": Decimal("11.00")}, {"id": 8, "price": Decimal("5.50")}]

# Объекты модели — со всеми полями, сохранённые.
books = await Book.objects.filter(author=old).update(author=new).returning()

# Удалённые строки.
gone = await Loan.objects.filter(due_at__lt=now).delete().returning("id", "reader")
```

```sql
UPDATE "book" SET "price"="price"*$1 WHERE "copies"<$2 RETURNING "id","price"
```

- **Поля.** Обычное поле модели, прямая связь (`"reader"` — её ключ, кортеж для составного) или `"pk"`
  (кортеж для составного первичного ключа); значения раскодируются так, как их читает поле. Путь через
  связь, обратная связь или «многие-ко-многим», неизвестное имя или имя, названное дважды, дают
  `FieldError` при вызове `returning()`.
- **Без полей** возвращаются объекты модели: запись возвращает первичные ключи, а строки читаются по ним
  в транзакции записи — у каждого объекта загружены все поля, как у обычного запроса.
- **Обновление** возвращает каждую строку в записанном виде: новые значения, поля `auto_now` и
  увеличенную версию `Meta.optimistic_lock_field`. Значение, записанное из выражения, возвращается
  таким, каким его сохранила база. Фильтр через связь
  работает так же, как у `update()`; срез отклоняется, как у `update()`.
- **Удаление** возвращает каждую строку такой, какой она была при удалении. Обычный `DELETE`, который
  база выполняет сама, возвращает названные поля собственным `RETURNING`. Удаление, которое hare
  выполняет в Python, — `on_delete`, который база не обеспечивает, модель с циклом каскада, модель с
  переопределённым `delete()` (или `hard_delete()`), объекты —
  сначала читает подходящие строки и удаляет их в одной транзакции. Мягкое удаление
  (`Meta.soft_delete_field`) возвращает строки после него: с заполненным временем удаления; уже мягко
  удалённая строка повторно не удаляется и не возвращается (`include_deleted().delete()`).
  `hard_delete().returning()` удаляет по-настоящему.
- Ни одна строка не подошла — пустой список. `none()` не отправляет команду.
- Порядок строк — порядок базы (`RETURNING`), иначе порядок, в котором удаление их нашло.
- `returning()` требует `features.supports_returning` (PostgreSQL и SQLite): на другой базе (ClickHouse)
  даёт `UnSupportedError` до отправки SQL. `old=` требует самого `RETURNING`. Команда сохраняет свой [план](query-plan-cache.ru.md),
  а возвращаемые поля входят в его ключ.
- **Значения до обновления.** `update(...).returning(*fields, old=(...))` возвращает ещё и поля из `old`
  такими, какими они были до обновления, под ключом `"old"` — изменение каждой строки в той же команде
  (PostgreSQL 18, `RETURNING old.column`):

    ```python
    rows = await Reader.objects.filter(id__in=ids).update(fines=F("fines") - payment).returning(
        "id", "fines", old=("fines",)
    )
    # [{"id": 7, "fines": Decimal("0.50"), "old": {"fines": Decimal("3.00")}}, ...]
    ```

    `old` принимает те же имена, что и поля, и требует названных полей (у объектов модели нет места для
    старых значений); `"old"` тогда не может быть среди названных полей. Сервер без
    `features.supports_returning_old_new` (PostgreSQL до 18, SQLite) даёт `UnSupportedError` до отправки
    SQL; `merge().returning()` принимает `old=` так же.

## <a id="insert-from"></a>Вставка строк запроса

```python
class QuerySet(QuerySpecification):
    def insert_from(self, queryset: QuerySet[Any, Any], *, fields: Sequence[str]) -> InsertFromQuery: ...
```

`insert_from()` записывает строки, которые выбирает запрос, в таблицу модели одной командой — строки не
попадают в Python. Возвращает число записанных строк:

```python
written = await ArchivedLoan.objects.insert_from(
    Loan.objects.filter(returned_at__isnull=False).values("book_id", "reader_id", archived=F("returned_at")),
    fields=["book", "reader", "archived_at"],
)
```

```sql
INSERT INTO "archivedloan" ("book_id","reader_id","archived_at","created")
SELECT "book_id","reader_id","returned_at",$1 FROM "loan" WHERE "returned_at" IS NOT NULL
```

- **`fields`** — записываемые поля в том порядке, в каком источник называет колонки: обычное поле или
  прямая связь с ключом из одной колонки (`"book"` записывает колонку её ключа). Связь с составным ключом
  записывается через поля её ключа; неизвестное, вычисляемое базой или «многие ко многим» поле, а также
  поле, названное дважды, дают `FieldError` при вызове.
- **Источник** — запрос `values()`/`values_list()`, выбирающий по одной колонке на поле (в том числе
  выражения и вычисляемые значения, `values(total=F("price") * F("quantity"))`), запрос объектов модели
  (он выбирает свои поля с теми же именами) или `union()` таких запросов. Фильтры, соединения,
  сортировка, срезы и `distinct()` работают как при чтении. Источник, выбирающий другое число колонок,
  даёт `QueryError` до отправки SQL. Источник выполняется на подключении целевой модели.
- **Неназванные поля** получают значение по умолчанию базы (`db_default`, автоинкрементный ключ). Поле
  `auto_now`/`auto_now_add` получает момент вставки — один на все строки. Python-значение `default=`
  (функция, константа) не вычисляется — назовите поле или задайте ему `db_default`.
- **Арендаторы.** Неназванное `Meta.tenant_field` записывается как активный арендатор (`QueryError`, если
  область видимости не из одного арендатора, как у `create()`). При активной области арендатор нельзя
  читать из строк источника, как и связь с моделью с арендаторами — вставка не может проверить их по
  строкам, — это даёт `QueryError`; при `all_tenants()` у целевой модели строки источника записываются
  как есть.
- Строки обратно не читаются: объекты не создаются, `save()` не вызывается, сигналы объектов модели не
  отправляются; наблюдатель `RowsChanged` получает вставку без ключей.
- **ClickHouse** не хранит уникальность: строки записываются такими, какими их даёт источник, строка с
  повторённым ключом — это ещё одна строка. Модели, чей `Meta.change_capture` читает записанные строки,
  там нужен первичный ключ среди `fields` — база не возвращает строк записи, — иначе `UnSupportedError`.
- Объединённый целевой запрос (`union()`/...) даёт `QueryError`. Для команды
  [план](query-plan-cache.ru.md) не хранится.

## <a id="merge"></a>Слияние строк

```python
class QuerySet(QuerySpecification):
    def merge(self, source: Any, *, on: str | Sequence[str] | Mapping[str, str]) -> MergeQuery: ...
```

`merge()` сопоставляет строки источника со строками модели и записывает их одной командой `MERGE` — по
веткам, в порядке их добавления: найденная строка обновляется, удаляется или остаётся как есть, строка
источника без пары вставляется или пропускается, а (PostgreSQL 17+) строка модели, которой не нашлось
пары в источнике, обновляется, удаляется или остаётся как есть. Возвращает число записанных строк.

```python
from hare.query.expressions import F, Q

written = await (
    Inventory.objects.merge(
        [{"isbn": "9780441013593", "copies": 5}, {"isbn": "9780553293357", "copies": 2}],
        on="isbn",
    )
    .when_matched(delete=True, condition=Q(copies__lte=F("merge_source__copies")) & Q(merge_source__copies__lt=0))
    .when_matched(update={"copies": F("copies") + F("merge_source__copies")})
    .when_not_matched(insert={"isbn": F("merge_source__isbn"), "copies": F("merge_source__copies")})
)
```

```sql
MERGE INTO "inventory" USING (SELECT column1 AS "isbn", column2 AS "copies"
  FROM (VALUES (CAST($1 AS VARCHAR), CAST($2 AS INTEGER)), (...)) AS "hare_merge_values") AS "hare_merge_source"
ON "inventory"."isbn"="hare_merge_source"."isbn"
WHEN MATCHED AND ... THEN DELETE
WHEN MATCHED THEN UPDATE SET "copies" = "inventory"."copies"+"hare_merge_source"."copies", "version" = "inventory"."version"+$3
WHEN NOT MATCHED THEN INSERT ("isbn", "copies") VALUES ("hare_merge_source"."isbn", "hare_merge_source"."copies")
```

- **Источник** — список словарей с одинаковыми ключами (каждый ключ — колонка; ключ с именем поля модели
  приводится к его типу, связанный объект записывается своим ключом), запрос `values()` (его имена —
  колонки), запрос объектов модели (все его поля) или `union()` запросов `values()`. Запрос
  `values_list()` даёт `QueryError` — у его колонок нет имён. Пустой список не отправляет команду, если
  нет ветки `when_not_matched_by_source()`: она берёт строки модели, которым тогда не соответствует ни
  одна строка источника.
- **`on`** — сопоставляемые поля: имя поля, список имён или словарь «поле модели → колонка источника»
  (`on={"isbn": "code"}`). Поле — обычное поле или прямая связь с ключом из одной колонки; колонка
  источника, которой в нём нет, даёт `QueryError` до отправки SQL.
- **Строка источника** читается как `merge_source__<column>` — в значениях ветки
  (`F("merge_source__copies")` и выражения от него) и в её условии (`Q(merge_source__copies__gt=0)`, любой
  оператор фильтра). `F("<field>")` и обычные ключи `Q` читают строку модели. Колонка, которой нет в
  источнике, даёт `FieldError`; модель с полем `merge_source` даёт `QueryError`.
- **Ветки** — `when_matched(update=... | delete=True | do_nothing=True, condition=Q)`,
  `when_not_matched(insert=... | do_nothing=True, condition=Q)`,
  `when_not_matched_by_source(update=... | delete=True | do_nothing=True, condition=Q)`: ровно одно
  действие в каждой. Строка попадает в первую ветку, условие которой выполняется; `do_nothing` не даёт
  взять её следующим веткам. Значения — обычные значения, связанные объекты, `F()` и выражения; значение
  приводится и проверяется полем так же, как у `update()`.
- **Читаемые строки модели** — строки целевого запроса: его фильтры
  (`Inventory.objects.filter(branch=branch).merge(...)`) и область видимости по умолчанию (мягко
  удалённые строки, строки других арендаторов) ограничивают строки, с которыми может совпасть строка
  источника, — скрытая строка считается отсутствующей, и её строка источника идёт в `when_not_matched()`.
  `when_not_matched_by_source()` тоже берёт только эти строки. Целевой запрос не может вычислять
  значения, брать срез, `distinct()`, `group_by()`, `sample()` и `with_cte()`, а фильтр — проходить через
  связь (`QueryError`).
- **Что заполняется само.** Обновление ставит полям `auto_now` момент команды и увеличивает
  `Meta.optimistic_lock_field`; вставка заполняет поля `auto_now`/`auto_now_add` и, при области видимости
  из одного арендатора, `Meta.tenant_field`. Python-значение `default=` при вставке не вычисляется —
  назовите поле или задайте ему `db_default`. Обновление отказывает в том же, в чём `update()`, — первичный
  ключ, `Meta.optimistic_lock_field`, `Meta.soft_delete_field` (`QueryError`); вставка пишет их, как
  `create()`. Зашифрованное поле со слепым индексом принимает простое значение или `F()` другого такого поля.
  Значения берутся такими, какими они были при добавлении ветки.
- **Арендаторы.** При активной области арендатор, записываемый из выражения, обновление, выводящее строку
  из области, или связь с моделью с арендаторами, записываемая из выражения, дают `QueryError`; связанные
  объекты, записанные обычными значениями, проверяются по области их модели. При `all_tenants()` у
  целевой модели значения записываются как есть.
- **Удаление** выполняет сама база: модель с `Meta.soft_delete_field`, `on_delete`, который база не
  обеспечивает, `PROTECT` или захват изменённых строк (`Meta.change_capture`) дают `QueryError` для ветки
  `delete=True` — используйте `delete()`.
- **`.returning(*fields)`** (PostgreSQL 17+) возвращает записанные строки: словарь названных полей и
  `"merge_action"` — `"insert"`, `"update"` или `"delete"` (удалённая строка — такой, какой была). Нужно
  назвать хотя бы одно поле. `old=("copies",)` добавляет эти поля такими, какими они были до слияния, под
  ключом `"old"` — None у вставленной строки (PostgreSQL 18, [как у `update()`](#returning)).
- PostgreSQL 15+ (`features.supports_merge`); `when_not_matched_by_source()` и `returning()` требуют
  PostgreSQL 17 (`supports_merge_not_matched_by_source`, `supports_merge_returning`). На другой базе или
  более старом сервере слияние даёт `UnSupportedError` до отправки SQL. Слияние без веток даёт
  `QueryError`. Для команды [план](query-plan-cache.ru.md) не хранится.
