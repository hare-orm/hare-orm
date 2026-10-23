# Ограничения и триггеры

Ограничения, которые модель объявляет в `Meta.constraints`, и триггеры из `Meta.triggers` создаются
вместе с таблицей — и через `generate_schemas()`, и миграциями, — а меняются и удаляются вместе со своими
объявлениями.

```python
from hare.ddl import (
    CheckConstraint, ExclusionConstraint, ExclusionConstraintUsing, RawSQLTerm, Trigger, TriggerEvent,
    TriggerForEach, TriggerTiming, UniqueConstraint,
)
```

## <a id="constraints"></a>Ограничения

Два класса ограничений из `hare.ddl`, которые объявляются в `Meta.constraints`. В ClickHouse
`CheckConstraint` работает, а `UniqueConstraint` (как и `unique=True`) отклоняет модель при привязке
(`ConfigurationError`) — ClickHouse ничего не хранит уникальным; см. [ClickHouse: уникальность и
связи](../dialects/clickhouse/models.ru.md#uniqueness-and-relations).

```python
@dataclass(frozen=True)
class UniqueConstraint:
    fields: tuple[str, ...]
    name: str | None = None
    condition: Q | RawSQLTerm | None = None  # Q или готовый SQL — частичный уникальный индекс (PostgreSQL и SQLite)
    deferrable: bool = False           # только PostgreSQL; не вместе с condition
    initially_deferred: bool = False   # требует deferrable=True
    include: tuple[str, ...] = ()      # неключевые колонки INCLUDE (PostgreSQL; SQLite создаёт индекс без них)
    nulls_distinct: bool | None = None # PostgreSQL 15+: False = NULLS NOT DISTINCT
    without_overlaps: bool = False     # PostgreSQL 18+: последнее поле — диапазон, который не должен пересекаться у строк

@dataclass(frozen=True)
class CheckConstraint:
    check: Q | RawSQLTerm   # Q по собственным полям модели или готовый SQL
    name: str
```

```python
class Book(Model):
    ...
    class Meta:
        constraints = (
            CheckConstraint(name="book_valid", check=Q(pages__gt=0) & ~Q(title="")),
            UniqueConstraint(fields=("isbn",), name="uq_book_isbn_in_print", condition=Q(in_print=True)),
        )
```

### <a id="unique-constraint"></a>`UniqueConstraint`

`fields` называет одно поле модели с колонкой или больше — пустой кортеж, имя, которое не поле, или
`ManyToManyField` дают `ConfigurationError`. Без `name` имя создаётся автоматически.

При `nulls_distinct=False` строки, в которых поля равны или равны `NULL`, считаются одинаковыми:
`(NULL, 1)` дважды нарушает `UniqueConstraint(fields=("shelf", "position"), nulls_distinct=False)`;
`True` явно указывает поведение по умолчанию. Это есть только в PostgreSQL (15+): модель, которая
объявляет его на другой базе, даёт `ConfigurationError`, когда её привязывают к подключению
(`Hare.init()`, `makemigrations`), — до того, как будет записана миграция, которую не смог бы применить
ни один `migrate`. `include` сохраняет в индексе ограничения дополнительные колонки, поэтому запрос,
который читает только их и ключ, получает ответ из одного индекса.

`without_overlaps=True` делает последнее поле диапазоном, который не должен пересекаться у строк, равных
по остальным полям; для него нужны два поля или больше и не нужен `condition` (`ConfigurationError`).
`deferrable=True` вместе с `condition` тоже даёт `ConfigurationError`, а в SQLite `deferrable=True` даёт
`UnSupportedError` при создании таблицы.

### <a id="conditions"></a>Условия: `Q` и `RawSQLTerm`

Условие — у `UniqueConstraint`, `CheckConstraint`, `PartialIndex` или `ExclusionConstraint` — это `Q` по
собственным полям модели или готовый SQL в `RawSQLTerm(...)`, который пишется в команду создания как
есть. Всё остальное (простая строка, словарь) даёт `ConfigurationError`. `Q` переводится в SQL для той
базы, где выполняются команды создания, а значения подставляются прямо в текст:

```python
class Book(Model):
    ...
    class Meta:
        constraints = (CheckConstraint(name="book_priced_or_free", check=RawSQLTerm("price IS NOT NULL OR is_free")),)
        indexes = (PartialIndex(fields=("title",), condition=Q(price__gt=Decimal("1")) | Q(pages__lt=3)),)
```

Работают все операторы фильтра, `F()` и выражения над собственными полями модели (`F("low") + 1`,
`Upper("code")`); оператор через связь, агрегат, `Exists(...)` и пустой `Q()` дают `ConfigurationError`.
Файлы миграций хранят сам `Q`: `RenameField`
переименовывает поле внутри него, а `RemoveField` удаляет `CheckConstraint`, `Q` которого читает только
удаляемое поле (если он читает и другое поле, будет ошибка). Готовый SQL никогда не читается: поле, которое
он называет, удаляется и переименовывается так, будто не называет, — поменяйте готовый SQL в модели, и
`makemigrations` удалит и добавит объявление вокруг изменения поля.

В SQLite:

- текстовый оператор (`contains`, `startswith`, `iendswith` и другие) пишется как `GLOB`, который находит
  те же строки с учётом регистра: при `case_sensitive_like` SQLite не принимает свой `LIKE` в таком
  условии; шаблон, взятый из другой колонки, даёт `UnSupportedError`;
- `Decimal` сравнивается как число;
- оператор, который в SQLite есть только через собственные функции hare (операторы `JSONField`,
  регулярные выражения и т. п.), даёт `UnSupportedError`: вне подключений hare такой функции не
  существует (см. [В ограничениях и
  индексах](../dialects/sqlite/functions.ru.md#in-constraints-and-indexes)).

### <a id="exclusion-constraint"></a>`ExclusionConstraint` (PostgreSQL)

`ExclusionConstraint` — это `EXCLUDE USING <method> (...)`: оно отклоняет строку, сочетание значений
которой пересекается с уже существующей, без проверки запросом перед каждой записью.

```python
class ExclusionConstraintUsing(StrEnum):
    GIST = "gist"; SPGIST = "spgist"; BTREE = "btree"; HASH = "hash"

@dataclass(frozen=True)
class ExclusionConstraint:
    name: str
    expressions: tuple[tuple[str | Expression | RawSQLTerm, str], ...]  # пары (выражение, оператор)
    using: ExclusionConstraintUsing = ExclusionConstraintUsing.GIST
    condition: Q | RawSQLTerm | None = None
    include: tuple[str, ...] = ()        # дополнительные колонки INCLUDE в индексе ограничения
    deferrable: bool = False             # проверяется при фиксации транзакции (или после SET CONSTRAINTS ... DEFERRED)
    initially_deferred: bool = False     # требует deferrable=True
```

Обычное применение — периоды, которые не должны пересекаться: книгу нельзя выдать дважды одновременно:

```python
from hare.dialects.postgresql.fields import DateTimeRangeField


class Loan(Model):
    book = fields.ForeignKeyField("models.Book")
    during = DateTimeRangeField()

    class Meta:
        constraints = (
            ExclusionConstraint(
                name="no_overlapping_loans",
                expressions=(("book", "="), ("during", "&&")),
                using=ExclusionConstraintUsing.GIST,
            ),
        )
```

Элемент `expressions` — имя поля (связь означает колонку её ключа), выражение hare над полями модели —
`(Lower("code"), "=")`, — которое пишется SQL PostgreSQL и переименовывается вместе с полями, которые оно
читает, или `RawSQLTerm`, записываемый как есть.

Отложенное (`deferrable`) ограничение исключения позволяет транзакции пройти через состояние с
пересечением — например, поменять местами две выдачи, — если к моменту фиксации пересечений не осталось.

У индекса GiST нет своего класса операторов для обычной скалярной колонки (ключа `book` выше, числа,
текста, даты, uuid и т. п.) — его даёт расширение `btree_gist`. Ограничению `ExclusionConstraint` с
методом GiST, в котором есть такое поле, оно нужно, поэтому hare создаёт его так же, как `citext` для
`CitextField`: `generate_schemas()` выполняет `CREATE EXTENSION btree_gist`, а автоматическое создание
миграций добавляет `CreateExtension("btree_gist")` — записывать его в `Meta.extensions` не нужно. Тип
выражения `RawSQLTerm` неизвестен, поэтому если оно даёт скалярное значение, добавьте `"btree_gist"` в
`Meta.extensions` сами. Ограничение `HASH` принимает ровно одно выражение с `=` и без `include` — у
хеш-индекса один ключ и нет колонок `INCLUDE`; другое объявление даёт `ConfigurationError`.

Вместо имени поля можно указать готовое выражение `RawSQLTerm` — для вычисляемого значения, которого нет
среди собственных полей модели:

```python
ExclusionConstraint(
    name="no_overlapping_reader_loans",
    expressions=((RawSQLTerm("lower(reader_email)"), "="), ("during", "&&")),
)
```

> [!WARNING]
> `ExclusionConstraint.expressions` не может ссылаться на колонку присоединённой таблицы: готовое
> выражение работает только с собственной таблицей модели. Правило, которому нужна колонка другой
> таблицы, проверяется в коде приложения (внутри транзакции, с блокировкой строк).

## <a id="triggers"></a>Триггеры

В ClickHouse триггеров нет: модель, объявившая триггер, даёт `UnSupportedError` до DDL.

```python
class TriggerEvent(StrEnum): INSERT = "INSERT"; UPDATE = "UPDATE"; DELETE = "DELETE"
class TriggerTiming(StrEnum): BEFORE = "BEFORE"; AFTER = "AFTER"; INSTEAD_OF = "INSTEAD OF"  # PostgreSQL, только для представлений
class TriggerForEach(StrEnum): ROW = "ROW"; STATEMENT = "STATEMENT"  # SQLite: только ROW

@dataclass(frozen=True)
class Trigger:
    name: str
    on: str              # элемент TriggerEvent или текст вроде "INSERT OR UPDATE OF parent_id"
    body: RawSQLTerm      # всё тело триггера
    timing: TriggerTiming = TriggerTiming.AFTER
    for_each: TriggerForEach = TriggerForEach.ROW
    when: Q | RawSQLTerm | None = None   # условие WHEN
    language: str | None = None          # язык диалекта по умолчанию (PostgreSQL: plpgsql)
    deferrable: bool = False           # CONSTRAINT TRIGGER в PostgreSQL
    initially_deferred: bool = False   # требует deferrable=True
    enabled: bool = True               # False: есть, но не срабатывает (PostgreSQL)
```

```python
class Category(Model):
    ...
    class Meta:
        triggers = (
            Trigger(
                name="trg_category_depth",
                on=TriggerEvent.INSERT,
                timing=TriggerTiming.BEFORE,
                body=RawSQLTerm("""
                    IF NEW.parent_id IS NULL THEN
                        NEW.depth := 1;
                    ELSE
                        SELECT depth + 1 INTO NEW.depth FROM "category" WHERE id = NEW.parent_id;
                    END IF;
                    RETURN NEW;
                """),
            ),
        )
```

В PostgreSQL создаются сразу и функция `CREATE FUNCTION ... RETURNS TRIGGER`, и `CREATE TRIGGER`, который
её вызывает, — без функции триггер подключить нельзя. В SQLite `body` вставляется прямо в
`CREATE TRIGGER ... BEGIN ... END`, отдельной функции нет.

Триггер из `Meta.triggers` срабатывает на таблице модели, поэтому `INSTEAD_OF`, который база выполняет
только на представлении, даёт `ConfigurationError` при определении модели; модель `Meta.managed = False`
поверх представления может его объявить.

### <a id="trigger-body-and-when"></a>`body` и `when`

`body` и готовое условие `when` — это `RawSQLTerm(...)`, как готовое условие ограничения или индекса;
обычный текст отклоняется с `ConfigurationError`. Вместо этого `when` может быть `Q` по собственным полям
модели: он сравнивает строку, для которой срабатывает триггер, — `NEW`, или `OLD` у триггера только на
`DELETE`, — со значениями, вписанными в DDL, и следует за `RenameField`/`RemoveField`, как условие
ограничения (поле, которое называет условие триггера, удалить нельзя). Условию `Q` нужен построчный
триггер и одна строка события: триггер `STATEMENT` или триггер на `DELETE` вместе с другим событием
принимает `RawSQLTerm(...)`.

```python
Trigger(
    name="trg_book_stock_check",
    on=TriggerEvent.UPDATE,
    timing=TriggerTiming.BEFORE,
    when=Q(stock__lt=0),
    body=RawSQLTerm("RAISE EXCEPTION 'stock can''t go negative'; RETURN NEW;"),
)
```

`inspectdb` и `drift` читают `WHEN` триггера SQLite, записанный со скобками вокруг и без них
(`WHEN OLD.price <> NEW.price`, `WHEN (OLD.price <> NEW.price)`).

### <a id="deferrable-triggers"></a>Отложенные триггеры (PostgreSQL)

`deferrable=True` создаёт в PostgreSQL `CREATE CONSTRAINT TRIGGER` вместо обычного `CREATE TRIGGER`: его
проверку можно отложить до конца транзакции (`SET CONSTRAINTS ... DEFERRED`), а не выполнять сразу после
каждой строки. `initially_deferred=True` делает отложенную проверку поведением по умолчанию в каждой
транзакции, без явного `SET CONSTRAINTS`. Оба требуют `timing=TriggerTiming.AFTER` и
`for_each=TriggerForEach.ROW` — триггер-ограничение не может быть `BEFORE` или срабатывать на всю команду
(`ConfigurationError`), — а SQLite даёт `UnSupportedError` для `deferrable=True`, потому что такого
понятия в нём нет.

### <a id="disabled-triggers"></a>Выключенный триггер (PostgreSQL)

`enabled=False` создаёт триггер и отключает его (`ALTER TABLE ... DISABLE TRIGGER`): он остаётся в базе,
но не срабатывает; SQLite, где срабатывает каждый триггер, даёт `UnSupportedError`.
