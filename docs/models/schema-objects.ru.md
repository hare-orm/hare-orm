# Представления, функции, последовательности и доступ

Модель объявляет в своём `Meta` объекты базы данных, которые живут рядом с её таблицей:
представления, материализованные представления, функции, последовательности, защиту на уровне строк
с её политиками и права ролей. Их создают `Hare.generate_schemas()` и миграции — `makemigrations`
пишет операцию на каждый добавленный, изменённый, переименованный или удалённый объект (см.
[Операции](../migrations/operations.ru.md#schema-objects)), — а удаляются они вместе с моделью.

Представления и материализованные представления есть в PostgreSQL и ClickHouse (см.
[Представления ClickHouse](../dialects/clickhouse/schema-objects.ru.md)); функции, последовательности,
защита на уровне строк и права — только в PostgreSQL. На базе без такого объекта (SQLite, а для
последних — и ClickHouse) модель, объявившая его, и любая операция над ним вызывают
`UnSupportedError` до отправки SQL — см. признаки `Features` в
[Диалекты и возможности](../dialects/dialects-and-features.ru.md).

```python
from hare import Model, fields
from hare.ddl import (
    DatabaseFunction,
    DatabaseSequence,
    FunctionVolatility,
    Grant,
    GrantTarget,
    MaterializedView,
    Policy,
    PolicyCommand,
    Privilege,
    RawSQLTerm,
    RowLevelSecurity,
    View,
)


class Invoice(Model):
    id = fields.IntField(primary_key=True)
    number = fields.BigIntField(null=True)
    tenant_id = fields.IntField()
    amount = fields.IntField()
    paid = fields.BooleanField(default=False)

    class Meta:
        sequences = [DatabaseSequence("invoice_number", start=1000, owned_by="number")]
        functions = [
            DatabaseFunction(
                "current_tenant",
                returns="integer",
                body=RawSQLTerm("SELECT nullif(current_setting('app.tenant', true), '')::integer"),
                language="sql",
                volatility=FunctionVolatility.STABLE,
            ),
        ]
        views = [View("paid_invoices", query=lambda: Invoice.objects.filter(paid=True).values("id", "amount"))]
        materialized_views = [
            MaterializedView(
                "invoice_totals",
                query=RawSQLTerm("SELECT tenant_id, sum(amount) AS total FROM invoice GROUP BY tenant_id"),
                unique_columns=("tenant_id",),
            ),
        ]
        row_level_security = RowLevelSecurity.ENABLED
        policies = [
            Policy(
                "tenant_rows",
                command=PolicyCommand.SELECT,
                roles=("reporting",),
                using=RawSQLTerm("tenant_id = current_tenant()"),
            ),
        ]
        grants = [
            Grant(privileges=(Privilege.SELECT,), roles=("reporting",)),
            Grant((Privilege.SELECT,), ("reporting",), on=GrantTarget.VIEW, object_name="paid_invoices"),
        ]
```

Каждый объект создаётся в схеме модели (`Meta.schema`), как и её таблица. У представления,
материализованного представления, функции и последовательности своё имя в схеме: две модели,
объявившие такой объект с одним именем (например, абстрактная база объявила его для подклассов),
вызывают `ConfigurationError`, как и одна модель с представлением, материализованным представлением и
последовательностью одного имени. `Meta` принимает список или кортеж каждого вида; элемент другого
класса или два элемента одного имени (два одинаковых права) вызывают `ConfigurationError` при
объявлении модели. Объявление само проверяет свои аргументы — тип и диапазон — и вызывает
`ConfigurationError` для неверного.

## <a id="views"></a>Представления

```python
@dataclass(frozen=True)
class View:
    name: str
    query: RawSQLTerm | QuerySet | Callable[[], QuerySet]
```

Именованный `SELECT`. `query` — QuerySet, функция, возвращающая QuerySet, или `RawSQLTerm` с готовым
SQL; обычный текст вызывает `ConfigurationError`. Функцией
`Meta` модели называет QuerySet самой модели, которая, пока выполняется её `Meta`, ещё не определена.
Queryset записывается как SQL со значениями внутри (`queryset.sql(parameters_inline=True)`) для
подключения, на котором он выполняется: состояние миграций и файл миграции хранят этот SQL, поэтому
QuerySet, чей SQL изменился (переименовано поле, переименована таблица модели), заставляет
`makemigrations` написать `AlterView`.

Изменение представления удаляет его и создаёт новую версию, затем снова выдаёт на неё права
`Meta.grants` модели — у заново созданного представления прав нет. Представление, которое читает
другое представление, так удалить нельзя; сначала уберите читающее представление в той же миграции.

## <a id="materialized-views"></a>Материализованные представления

```python
@dataclass(frozen=True)
class MaterializedView(View):
    name: str
    query: RawSQLTerm | QuerySet | Callable[[], QuerySet]
    with_data: bool = True
    unique_columns: tuple[str, ...] = ()
```

Строки запроса, хранящиеся в базе до обновления. `with_data=False` создаёт представление пустым и
недоступным для чтения до первого обновления. По `unique_columns` — колонкам представления, а не полям
модели, — строится уникальный индекс `<представление>_unique`, переименуемый вместе с представлением;
он нужен для обновления без блокировки чтения.

Обновление заново наполняет представление строками его запроса:

```python
await Invoice.objects.refresh_materialized_view("invoice_totals")
await Invoice.objects.refresh_materialized_view("invoice_totals", concurrently=True)
```

Обновление выполняется на подключении, в которое пишет модель (`.using(...)` выбирает другое), внутри
текущей транзакции, если она есть. `concurrently=True` оставляет представление доступным для чтения во
время обновления — для этого нужны `unique_columns` и уже наполненное представление (без
`unique_columns` — `ConfigurationError`). Представление, которого модель не объявляет, или
`concurrently` не типа bool вызывают `QueryError`. В миграции представление обновляет
[`RefreshMaterializedView`](../migrations/operations.ru.md#schema-objects).

Изменение материализованного представления удаляет его и создаёт новую версию — наполненную заново,
если не `with_data=False`, — и снова выдаёт на неё права модели.

## <a id="functions"></a>Функции

```python
@dataclass(frozen=True)
class DatabaseFunction:
    name: str
    returns: str                          # "integer", "SETOF text", "TABLE (id integer, total numeric)"
    body: RawSQLTerm
    arguments: tuple[str, ...] = ()       # ("tenant_id integer", "since date")
    language: str | None = None           # None: plpgsql в PostgreSQL
    volatility: FunctionVolatility = FunctionVolatility.VOLATILE
    security_definer: bool = False
```

Функция, хранимая в базе и вызываемая из SQL — из представления, условия политики, сырого запроса,
значения колонки по умолчанию. `body` — это `RawSQLTerm` с телом функции (обычный текст вызывает
`ConfigurationError`); `returns`, `arguments` и `body` пишутся в DDL как есть, поэтому они
непереносимы. Тело заключается в долларовые кавычки (`$hare_function$`); тело, содержащее эту кавычку,
вызывает `ConfigurationError`. `language` — простое имя; `volatility` — `VOLATILE`, `STABLE` или
`IMMUTABLE` — то, что функция обещает планировщику; `security_definer=True` выполняет её с правами
владельца.

Функцию узнают по имени и типам аргументов. Изменение, сохраняющее их и тип результата, заменяет её на
месте (`CREATE OR REPLACE FUNCTION`) с сохранением прав; изменение аргументов или типа результата
удаляет её и создаёт новую, снова выдавая на неё права модели. Функцию, от которой зависят
представление или политика, так удалить нельзя — сначала измените их в той же миграции. Функция
`LANGUAGE sql` при создании проверяется по таблицам, которые она читает, поэтому может читать только
уже существующие таблицы: функции модели создаются после таблиц миграции, до её представлений и
политик.

## <a id="sequences"></a>Последовательности

```python
@dataclass(frozen=True)
class DatabaseSequence:
    name: str
    start: int | None = None
    increment: int = 1
    minimum: int | None = None
    maximum: int | None = None
    cycle: bool = False
    cache: int = 1
    owned_by: str | None = None
```

Счётчик, выдающий номера. Каждый номер — 64-битное целое; `increment` не может быть 0 (отрицательный
считает вниз), `minimum` должен быть меньше `maximum`, `start` — между ними, а `cache` — сколько номеров
сеанс берёт заранее — от 1 до 1 000 000. Без `start` последовательность начинается с `minimum` при счёте
вверх (по умолчанию 1) и с `maximum` при счёте вниз. `owned_by` называет поле модели: последовательность
удаляется вместе с его колонкой, и у поля должна быть своя колонка.

Последовательности модели создаются до её таблицы, чтобы значение колонки по умолчанию могло брать из
них; колонка-владелец задаётся, когда таблица уже есть. Следующий номер читается во время работы на
подключении, в которое пишет модель:

```python
number = await Invoice.objects.get_next_sequence_value("invoice_number")
```

Выданный номер больше не выдаётся никогда, даже если транзакция откатилась. Последовательность,
которой модель не объявляет, вызывает `QueryError`.

Изменение последовательности сохраняет её текущий номер: новые шаг, границы, кэш, цикличность и
колонка-владелец действуют со следующего номера. `start` задаёт только место, с которого начнётся
перезапуск последовательности; не заданные граница или начало возвращаются к значениям по умолчанию.

## <a id="row-level-security"></a>Защита на уровне строк

```python
class Meta:
    row_level_security = RowLevelSecurity.ENABLED   # или RowLevelSecurity.FORCED, или None (выключена)
```

`ENABLED` фильтрует строки таблицы через её политики для всех ролей, кроме владельца; роль, которую не
пускает ни одна политика, не видит строк. `FORCED` применяет политики и к владельцу — это настройка для
приложения, которое подключается ролью-владельцем своих таблиц. `None` — значение по умолчанию —
выключает защиту. `ENABLED` без единой политики закрывает таблицу для всех остальных ролей: они читают
её строки только через функцию с `security_definer`. `Meta.policies` при выключенной
`row_level_security` вызывает `ConfigurationError`: база сохранила бы политики, но не применяла бы их.

```python
@dataclass(frozen=True)
class Policy:
    name: str
    command: PolicyCommand = PolicyCommand.ALL     # ALL, SELECT, INSERT, UPDATE, DELETE
    roles: tuple[str, ...] = ()                    # пусто: все роли
    using: Q | RawSQLTerm | TenantCondition | None = None
    with_check: Q | RawSQLTerm | TenantCondition | None = None
    permissive: bool = True
```

`using` — условие, которому должна отвечать строка, которую команда читает, изменяет или удаляет;
`with_check` — условие для строки, которую она вставляет или изменяет; без него политика `ALL` или
`UPDATE` проверяет `using`. Каждое — `Q` по собственным полям модели со значениями внутри или
`RawSQLTerm` с сырым SQL (вызов функции, чтение настройки) или `TenantCondition()` — `Meta.tenant_field` строки
среди арендаторов транзакции ([Разделение данных по арендаторам](../soft-delete-versions-tenants/multi-tenancy.ru.md#row-level-security)). Политике нужно хотя бы одно из них;
политика `INSERT` не принимает `using`, а `SELECT` и `DELETE` — `with_check`, иначе `ConfigurationError`.
Строка проходит, когда её пропускают любая разрешающая политика команды и все ограничивающие
(`permissive=False`). `PUBLIC`, `CURRENT_USER`, `CURRENT_ROLE` и `SESSION_USER` в `roles` пишутся
ключевыми словами; любая другая роль берётся в кавычки.

Имя политики уникально в пределах таблицы. Изменение её ролей или условий меняет её на месте; новая
команда или вид, а также убранное условие удаляют её и создают новую.

## <a id="grants"></a>Права

```python
@dataclass(frozen=True)
class Grant:
    privileges: tuple[Privilege, ...]
    roles: tuple[str, ...]
    on: GrantTarget = GrantTarget.TABLE      # TABLE, VIEW, MATERIALIZED_VIEW, SEQUENCE, FUNCTION
    object_name: str | None = None           # имя представления, последовательности или функции модели
    columns: tuple[str, ...] = ()            # поля — только для таблицы
    with_grant_option: bool = False
```

Права, которые получают роли, — на таблицу модели или на представление, материализованное
представление, последовательность или функцию, которые модель объявляет и которые называет
`object_name`. Права должны существовать для вида объекта:

| `on` | Права |
|---|---|
| `TABLE`, `VIEW` | `SELECT`, `INSERT`, `UPDATE`, `DELETE`, `TRUNCATE`, `REFERENCES`, `TRIGGER`, `ALL` |
| `MATERIALIZED_VIEW` | `SELECT`, `ALL` |
| `SEQUENCE` | `USAGE`, `SELECT`, `UPDATE`, `ALL` |
| `FUNCTION` | `EXECUTE`, `ALL` |

`columns` ограничивает `SELECT`, `INSERT`, `UPDATE` и `REFERENCES` на таблицу колонками некоторых
полей. `with_grant_option=True` позволяет ролям передавать права дальше. У права нет имени — его узнают
по всему, что оно выдаёт: изменённое право отзывается, а новое выдаётся. Удаление права отзывает его
привилегии у его ролей — даже если другое право модели выдаёт те же. Объект, который право называет,
но модель не объявляет, вызывает `ConfigurationError`, когда право выполняется. Роль должна существовать
на сервере до выдачи права — hare ролей не создаёт.

## <a id="in-migrations"></a>В миграциях

`makemigrations` сравнивает каждый вид объектов по имени: объект, у которого изменилось только имя,
переименовывается; изменённый под тем же именем — меняется (`AlterView`, `AlterFunction`,
`AlterSequence`, `AlterPolicy`, ...); остальные удаляются и добавляются; права отзываются и выдаются.
Операции идут в таком порядке, чтобы каждый объект находил то, чем пользуется:

- Сначала, до любых изменений полей и таблиц: отзыв прав, удаление политик, материализованных
  представлений и представлений, затем переименования.
- Затем операции моделей, полей, индексов и ограничений. `CreateModel` новой модели создаёт её
  последовательности до таблицы; остальные её объекты идут следом.
- Когда все таблицы на месте: создание и изменение последовательностей, функций, представлений,
  материализованных представлений, защиты строк, политик и прав — каждый вид после видов, которыми он
  может пользоваться.
- В конце: удаление функций и последовательностей.

Представление, материализованное представление или политика, чей SQL называет поле или колонку,
которые миграция удаляет или меняет, удаляется до операций полей и создаётся заново после них, даже
если в остальном не изменилось, — PostgreSQL не удаляет и не меняет колонку, от которой такой объект
зависит. hare находит такую ссылку по имени в запросе представления и в условиях политики.

Создание представления, материализованного представления, функции или последовательности сразу
выдаёт на неё права модели, поэтому объект, удалённый и созданный заново, сохраняет свои права.
Удаление модели удаляет её представления и материализованные представления до таблицы, затем её
функции и последовательности; её политики и права на таблицу уходят вместе с таблицей. Таблица, которую
PostgreSQL перестраивает (изменилось партиционирование), получает назад свои представления, защиту
строк, политики и права.
