# Ограничения и триггеры

## Ограничения {: #constraints }

```python
@dataclass(frozen=True)
class UniqueConstraint:
    fields: tuple[str, ...]
    name: str | None = None
    condition: Q | RawSQLTerm | None = None  # Q или готовый SQL — частичный уникальный индекс (PostgreSQL и SQLite)
    deferrable: bool = False           # только PostgreSQL; не вместе с condition
    initially_deferred: bool = False   # требует deferrable=True
    include: tuple[str, ...] = ()      # дополнительные колонки INCLUDE (PostgreSQL; SQLite создаёт индекс без них)
    nulls_distinct: bool | None = None # PostgreSQL 15+: False = NULLS NOT DISTINCT

@dataclass(frozen=True)
class CheckConstraint:
    check: Q | RawSQLTerm   # Q по собственным полям модели или готовый SQL
    name: str
```

```python
class Membership(Model):
    ...
    class Meta:
        constraints = (
            CheckConstraint(name="check_membership_scope", check=RawSQLTerm("team_id IS NOT NULL OR event_id IS NOT NULL")),
            UniqueConstraint(fields=("team_id",), name="uq_membership_open_team", condition=RawSQLTerm("status = 'open'")),
        )
```

При `nulls_distinct=False` строки, в которых поля равны или равны `NULL`, считаются одинаковыми:
`(NULL, 1)` дважды нарушает `UniqueConstraint(fields=("shelf", "pages"), nulls_distinct=False)`;
`True` явно указывает поведение по умолчанию. Это есть только в PostgreSQL: SQLite даёт для него
`UnSupportedError`. `include` сохраняет в индексе ограничения дополнительные колонки, поэтому запрос,
который читает только их и ключ, получает ответ из одного индекса.

Условие — это `Q` по собственным полям модели, как в Django, или готовый SQL в `RawSQLTerm(...)`
(`from hare.ddl import RawSQLTerm`), который пишется в команду создания как есть. Всё остальное
(простая строка, словарь) даёт `ConfigurationError`. `Q` переводится в SQL для той базы, где
выполняются команды создания, а значения подставляются прямо в текст:

```python
class Item(Model):
    ...
    class Meta:
        constraints = (
            CheckConstraint(name="item_valid", check=Q(qty__gte=0) & ~Q(name="")),
            UniqueConstraint(fields=("sku",), name="uq_item_active_sku", condition=Q(active=True)),
        )
        indexes = (PartialIndex(fields=("name",), condition=Q(price__gt=Decimal("1")) | Q(qty__lt=3)),)
```

Работают все операторы фильтра и `F()` собственных полей модели; оператор через связь, агрегат,
`Exists(...)` и пустой `Q()` дают `ConfigurationError`. Файлы миграций хранят сам `Q`: `RenameField`
переименовывает поле внутри него, а `RemoveField` удаляет `CheckConstraint`, `Q` которого читает
только удаляемое поле (если он читает и другое поле, будет ошибка — как и с готовым SQL). В SQLite
`Decimal` в таком условии сравнивается как число, а оператор, который в SQLite есть только через собственные
функции hare (операторы `JSONField`, регулярные выражения и т. п.), даёт `ConfigurationError`: вне
соединений hare такой функции не существует. `ExclusionConstraint.condition` тоже принимает `Q`.

В PostgreSQL есть ещё `ExclusionConstraint` — `EXCLUDE USING <метод> (...)`. Это встроенный в базу
способ отклонить строку, сочетание значений которой пересекается с уже существующей, вместо
ручной проверки запросом перед каждой записью. Классический пример — бронирования без пересечений:

```python
class ExclusionConstraintUsing(StrEnum):
    GIST = "gist"; SPGIST = "spgist"; BTREE = "btree"

@dataclass(frozen=True)
class ExclusionConstraint:
    name: str
    expressions: tuple[tuple[str | RawSQLTerm, str], ...]  # пары (имя поля или готовое SQL-выражение, оператор)
    using: ExclusionConstraintUsing = ExclusionConstraintUsing.GIST
    condition: Q | RawSQLTerm | None = None
    include: tuple[str, ...] = ()        # дополнительные колонки INCLUDE в индексе ограничения
    deferrable: bool = False             # проверяется при фиксации транзакции (или после SET CONSTRAINTS ... DEFERRED)
    initially_deferred: bool = False     # требует deferrable=True
```

Отложенное (`deferrable`) ограничение исключения позволяет транзакции пройти через состояние с
пересечением — например, поменять местами два бронирования, — если к моменту фиксации пересечений
не осталось.

```python
class Event(Model):
    team = fields.ForeignKeyField("models.Team")
    during = fields.DateTimeRangeField()

    class Meta:
        constraints = (
            ExclusionConstraint(
                name="no_overlapping_events",
                expressions=(("team", "="), ("during", "&&")),
                using=ExclusionConstraintUsing.GIST,
            ),
        )
```

У индекса GiST нет своего класса операторов для обычной скалярной колонки (ключа `team` выше,
числа, текста, даты, uuid и т. п.) — его даёт расширение `btree_gist`. Ограничению
`ExclusionConstraint` с методом GiST, в котором есть такое поле, оно нужно, поэтому hare создаёт его
так же, как `citext` для `CitextField`: `generate_schemas()` выполняет `CREATE EXTENSION btree_gist`,
а автоматическое создание миграций добавляет `CreateExtension("btree_gist")` — записывать его в
`Meta.extensions` не нужно. Тип выражения `RawSQLTerm` неизвестен, поэтому если оно даёт скалярное
значение, добавьте `"btree_gist"` в `Meta.extensions` сами.

Вместо имени поля можно указать готовое выражение `RawSQLTerm` — для вычисляемого значения, которого
нет среди собственных полей модели:

```python
ExclusionConstraint(
    name="no_overlapping_events_ci",
    expressions=((RawSQLTerm("lower(team_name)"), "="), ("during", "&&")),
)
```

!!! warning
    `ExclusionConstraint.expressions` не может ссылаться на колонку присоединённой таблицы: готовое
    выражение работает только с собственной таблицей модели. Если правило «пересечения» зависит от
    колонки другой таблицы, это ограничение не подходит — проверяйте правило в коде приложения
    (внутри транзакции, с блокировкой строк).

## Триггеры {: #triggers }

```python
class TriggerEvent(StrEnum): INSERT = "INSERT"; UPDATE = "UPDATE"; DELETE = "DELETE"
class TriggerTiming(StrEnum): BEFORE = "BEFORE"; AFTER = "AFTER"; INSTEAD_OF = "INSTEAD OF"  # PostgreSQL, только для представлений
class TriggerForEach(StrEnum): ROW = "ROW"; STATEMENT = "STATEMENT"  # SQLite: только ROW

@dataclass(frozen=True)
class Trigger:
    name: str
    on: str              # элемент TriggerEvent или текст вроде "INSERT OR UPDATE OF parent_id"
    body: str             # всё тело триггера
    timing: TriggerTiming = TriggerTiming.AFTER
    for_each: TriggerForEach = TriggerForEach.ROW
    when: str | None = None       # готовое SQL-условие WHEN
    language: str = "plpgsql"
    deferrable: bool = False           # CONSTRAINT TRIGGER в PostgreSQL
    initially_deferred: bool = False   # требует deferrable=True
```

`deferrable=True` создаёт в PostgreSQL `CREATE CONSTRAINT TRIGGER` вместо обычного `CREATE TRIGGER`:
его проверку можно отложить до конца транзакции (`SET CONSTRAINTS ... DEFERRED`), а не выполнять
сразу после каждой строки. `initially_deferred=True` делает отложенную проверку поведением по
умолчанию в каждой транзакции, без явного `SET CONSTRAINTS`. Оба требуют
`timing=TriggerTiming.AFTER` и `for_each=TriggerForEach.ROW` — триггер-ограничение не может быть
`BEFORE` или срабатывать на всю команду, — а SQLite даёт `UnSupportedError` для `deferrable=True`,
потому что такого понятия в нём нет.

```python
class Category(Model):
    ...
    class Meta:
        triggers = (
            Trigger(
                name="trg_category_depth",
                on=TriggerEvent.INSERT,
                timing=TriggerTiming.BEFORE,
                body="""
                    IF NEW.parent_id IS NULL THEN
                        NEW.depth := 1;
                    ELSE
                        SELECT depth + 1 INTO NEW.depth FROM "category" WHERE id = NEW.parent_id;
                    END IF;
                    RETURN NEW;
                """,
            ),
        )
```

В PostgreSQL создаются сразу и функция `CREATE FUNCTION ... RETURNS TRIGGER`, и `CREATE TRIGGER`,
который её вызывает, — без функции триггер подключить нельзя. В SQLite `body` вставляется прямо в
`CREATE TRIGGER ... BEGIN ... END`, отдельной функции нет.

**И `constraints`, и `triggers` создаются автоматически, когда выполняется операция миграции
`CreateModel`** — дописывать для них `RunSQL` вручную не нужно.
