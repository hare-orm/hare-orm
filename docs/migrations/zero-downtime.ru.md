# Миграции без остановки приложения

Миграция, которая применяется, пока приложение работает, не должна надолго блокировать большую таблицу
и не должна ломать код, который ещё работает во время выкладки, — старую версию, которая читает колонки и
таблицы такими, какими они были. hare проверяет миграции на то и другое ([проверка](#checking-migrations)),
а его операции делают каждое рискованное изменение маленькими безопасными шагами — приём «расширение,
затем сжатие» (expand-contract).

## <a id="checking-migrations"></a>Проверка миграций

**`makemigrations`** проверяет миграции, которые записывает, и печатает рискованное — каждое с тем, как
сделать то же изменение безопасно:

```text
WARNING: risky on a database in use if the tables are large - `hare checkmigrations` checks against the database:
  app.0007_remove_book_pages: Remove field pages from Book [remove_field]
    The column of Book.pages is dropped while the code still running reads it.
    Safely: First remove the field from the models only - SeparateDatabaseAndState(state_operations=[RemoveField(model_name='Book', name='pages')]) - and drop the column with RunSQL in a later migration, once no running code reads it.
```

Базу она не читает, поэтому любая таблица, которая существует до миграции, может оказаться большой.

**`hare checkmigrations [APP ...]`** проверяет миграции, которые применил бы `migrate`, в его порядке, по
самой базе: таблица считается большой от `migrations.safety.large_table_rows` строк (по умолчанию
100 000). PostgreSQL берёт оценку планировщика (`pg_class.reltuples`), а для таблицы, которую ни разу не
анализировали, сам считает строки, но не дальше порога; SQLite считает их так же; ClickHouse
берёт счётчик, который ведут его движки таблиц (`system.tables.total_rows`), — представление, у
которого счётчика нет, считается большим. Команда завершается с
кодом 1, пока есть риск без исключения, — запускайте её в CI перед выкладкой.

```python
# конфиг hare
{
    ...
    "migrations": {"safety": {"large_table_rows": 1_000_000}},
}
```

`large_table_rows` — целое число от 0 (любая таблица большая) до 10<sup>12</sup>; иное значение даёт
`ConfigurationError` при загрузке конфига.

Та же проверка из кода:

```python
from hare.migrations.api import checkmigrations

risks = await checkmigrations(config=HARE_ORM, app_labels=["shop"])
for risk in risks:
    print(risk.code, risk.app_label, risk.migration_name, risk.operation, risk.message, risk.safe_alternative)
```

Каждый `MigrationRisk` называет правило (`code`, значение `MigrationRiskCode`), миграцию и её операцию
(как её пишет `describe()`), что пойдёт не так (`message`) и безопасный способ (`safe_alternative`);
`exempted` — True для риска, который миграция принимает.

- Таблица, которую создаёт та же миграция, новая и пустая, — правила о блокировках к ней не применяются.
- В [`SeparateDatabaseAndState`](operations.ru.md#separate-database-and-state) операции базы проверяются
  правилами о блокировках; правила о работающем коде (переименованные и удалённые поля и модели) к ним не
  применяются — такая операция и есть способ вручную держать код и базу согласованными.
- `MigrationSafetyChecker(large_table_rows=...)` (`hare.migrations.safety`) проверяет одну миграцию на
  заданном состоянии: `await checker.check(migration, state, dialect=..., client=...)` — без `client`
  любая существующая таблица может быть большой.

### <a id="exempting-a-risk"></a>Исключение риска

Риск, проверенный вручную, — прочитанный SQL, таблица, которая заведомо останется маленькой, —
принимается: его код перечисляется в `safety_exemptions` миграции. `checkmigrations` всё равно его
показывает, с пометкой `(exempted)`, но из-за него не завершается с ошибкой:

```python
from hare import migrations
from hare.migrations import operations as ops
from hare.migrations.safety import MigrationRiskCode


class Migration(migrations.Migration):
    dependencies = [("shop", "0006_orders")]
    safety_exemptions = [MigrationRiskCode.RUN_SQL]
    operations = [ops.RunSQL("UPDATE shop_settings SET value = 'on' WHERE key = 'beta'")]
```

Элемент, который не является `MigrationRiskCode`, даёт `ConfigurationError`.

## <a id="rules"></a>Правила

Правила живут в диалектах (`Dialect.migration_safety_rules`): у каждой базы есть общие, PostgreSQL
добавляет правила своих блокировок. «Большая» ниже — таблица, в которой может быть `large_table_rows`
строк и больше.

| Код | Базы | Что находит |
| --- | --- | --- |
| `add_field_backfills_rows` | все | Поле NOT NULL только со значением по умолчанию на стороне Python в большой таблице |
| `add_field_volatile_default` | PostgreSQL | Поле с изменчивым значением по умолчанию в базе в большой таблице |
| `alter_field_rewrites_table` | все | Изменение поля, которое переписывает большую таблицу |
| `add_index_without_concurrently` | PostgreSQL | Индекс строится на большой таблице, пока запись в неё ждёт |
| `add_check_constraint_validates_rows` | PostgreSQL | Ограничение CHECK на большой таблице без `not_valid=True` |
| `add_foreign_key_validates_rows` | PostgreSQL | Внешний ключ на большой таблице без `not_valid=True` |
| `add_unique_constraint_builds_index` | PostgreSQL | Уникальное ограничение, индекс которого строится на большой таблице |
| `set_not_null_scans_table` | PostgreSQL | NOT NULL ставится сканированием большой таблицы под блокировкой |
| `rename_field` | все | Поле переименовано вместе с колонкой |
| `remove_field` | все | Поле удалено вместе с колонкой |
| `rename_model` | все | Таблица переименована |
| `delete_model` | все | Модель удалена вместе с таблицей |
| `run_sql` | все | SQL как есть |
| `schema_change_with_run_python` | все | Код Python в транзакции, которая меняет схему большой таблицы |

### <a id="add-field-backfills-rows"></a>`add_field_backfills_rows`

`AddField` поля NOT NULL, у которого значение по умолчанию есть только на стороне Python (`default=` или
`auto_now`), добавляет колонку с `NULL`, обновляет каждую существующую строку, затем делает колонку
NOT NULL — всё внутри миграции, держа таблицу. **Безопасно:** добавьте поле с `null=True`, заполните его
[`BackfillColumn`](#backfillcolumn-and-altercolumnnotnullsafe) в миграции с `atomic = False`, затем
сделайте NOT NULL через `AlterColumnNotNullSafe` ([приём 1](#adding-a-required-column)) — или дайте
постоянный `db_default`: база запомнит его один раз, не трогая строки.

### <a id="add-field-volatile-default"></a>`add_field_volatile_default`

Значение по умолчанию в базе, которое вызывает изменчивую функцию — `random()`, `gen_random_uuid()`,
`uuidv7()`, `clock_timestamp()`, `nextval()`..., — вычисляется для каждой существующей строки: PostgreSQL
переписывает таблицу. Постоянное значение или стабильная функция (`Now()`) запоминаются один раз.
**Безопасно:** добавьте поле с `null=True` без `db_default`, задайте `db_default` через `AlterField` (его
получат новые строки), заполните существующие строки `BackfillColumn`, затем `AlterColumnNotNullSafe`.

### <a id="alter-field-rewrites-table"></a>`alter_field_rewrites_table`

Изменение поля, которое база делает перезаписью таблицы; ни читать, ни писать её в это время нельзя.
Какое именно — решает редактор схемы диалекта (`rewrites_table_on_alter()`): PostgreSQL переписывает
таблицу при смене типа колонки, кроме типа, который вмещает каждое старое значение как есть, — более
длинный или неограниченный `varchar`, `varchar` в `text` и обратно в неограниченный, `numeric` с большим
числом цифр и тем же масштабом, `cidr` в `inet`; SQLite пересобирает таблицу при любом изменении, кроме
простого переименования и такого, после которого колонка объявлена как прежде (настройка на стороне
Python вроде `sensitive`, собственный индекс поля); ClickHouse переписывает при любой смене типа колонки.
**Безопасно:** добавьте поле нового вида, заполните его партиями, переведите на
него код и удалите старое ([приём 2](#renaming-a-column)).

### <a id="add-index-without-concurrently"></a>`add_index_without_concurrently`

`AddIndex` без `concurrently=True`, а также индекс поля, добавленного (или изменённого так, чтобы он
появился) с `db_index=True` — у внешнего ключа он есть по умолчанию, — строятся, пока запись в таблицу
ждёт. **Безопасно:** `AddIndex(..., concurrently=True)` в миграции с `atomic = False`
([приём 3](#adding-an-index)); для поля — объявите его с `db_index=False` и добавьте его индекс в
`Meta.indexes` этим способом.

### <a id="add-check-constraint-validates-rows"></a>`add_check_constraint_validates_rows`

`AddConstraint` с `CheckConstraint` проверяет каждую строку, пока таблицу нельзя ни читать, ни писать.
**Безопасно:** `AddConstraint(..., not_valid=True)` — проверяются только новые и изменённые строки — и
`ValidateConstraint` в следующей миграции; запись он не блокирует ([приём 4](#adding-a-constraint)).

### <a id="add-foreign-key-validates-rows"></a>`add_foreign_key_validates_rows`

`AddField` поля `ForeignKeyField`/`OneToOneField` с ограничением в базе проверяет каждую строку, пока
запись в обе таблицы ждёт; так же — `AlterField`, который даёт связи ограничение или другую цель.
**Безопасно:** `AddField(..., not_valid=True)` и позже `ValidateConstraint` с именем внешнего ключа
([приём 4](#adding-a-constraint)); `AlterField` — внутри `SeparateDatabaseAndState`, где ограничение
добавляется `NOT VALID` через `RunSQL`.

### <a id="add-unique-constraint-builds-index"></a>`add_unique_constraint_builds_index`

`AddConstraint` с `UniqueConstraint`, а также поле, добавленное или изменённое с `unique=True`, строят
уникальный индекс ограничения, пока запись в таблицу ждёт. **Безопасно:** сначала постройте индекс через
`AddIndex(..., unique=True, concurrently=True)`, затем пусть ограничение заберёт его себе:
`AddConstraint(..., using_index=...)` ([приём 4](#adding-a-constraint)). Уникальное ограничение с
`condition` забрать индекс не может — объявите его как уникальный `PartialIndex`.

### <a id="set-not-null-scans-table"></a>`set_not_null_scans_table`

`AlterField` с `null=True` на `null=False` сканирует таблицу, пока её нельзя ни читать, ни писать; так же
— `AlterColumnNotNullSafe` внутри транзакции. **Безопасно:** `AlterColumnNotNullSafe` в миграции с
`atomic = False` ([ниже](#backfillcolumn-and-altercolumnnotnullsafe)).

### <a id="rename-field"></a>`rename_field` и `rename_model`

Поле, переименованное вместе с колонкой (`RenameField`), модель, переименованная вместе с таблицей
(`RenameModel` модели без `Meta.table`), или изменённый `Meta.table` (`AlterModelTable`): код, который
ещё работает, обращается к старому имени и падает, пока его не заменят. Переименование, которое
сохраняет колонку или таблицу, в базе ничего не меняет и не показывается. **Безопасно:** сохраните имя в
базе — `source_field="<старая колонка>"` у поля, `Meta.table = "<старая таблица>"` у модели — или
перейдите на новую колонку ([приём 2](#renaming-a-column)).

### <a id="remove-field"></a>`remove_field` и `delete_model`

`RemoveField` удаляет колонку, `DeleteModel` — таблицу, пока работающий код их ещё читает.
**Безопасно:** сначала уберите их только из моделей —
`SeparateDatabaseAndState(state_operations=[RemoveField(...)])`, `DeleteModel(..., state_only=True)`, —
а колонку или таблицу удалите через `RunSQL` в следующей миграции, когда её уже не читает ни один
работающий код ([приём 5](#removing-a-field)).

### <a id="run-sql"></a>`run_sql`

`RunSQL` и `SQLOperation`: проверка не может сказать, какие таблицы и как надолго блокирует этот SQL.
**Безопасно:** прочитайте команды, затем [исключите](#exempting-a-risk) код в миграции.

### <a id="schema-change-with-run-python"></a>`schema_change_with_run_python`

`RunPython` в атомарной миграции, которая также меняет схему большой таблицы, на базе, где изменения
схемы транзакционны: блокировки, взятые изменениями схемы, держатся до конца транзакции — всё время, пока
работает код. **Безопасно:** вынесите `RunPython` в отдельную миграцию или поставьте миграции
`atomic = False`.

## <a id="backfillcolumn-and-altercolumnnotnullsafe"></a>BackfillColumn и AlterColumnNotNullSafe

```python
BackfillColumn(model_name: str, field_name: str, value: Any | Callable[[], Any], *, batch_size: int = 1000)
AlterColumnNotNullSafe(model_name: str, field_name: str)
```

`BackfillColumn` выполняет цикл ограниченных `UPDATE` (не больше `batch_size` строк за команду, пока
строки не закончатся) вместо одного огромного `UPDATE` по всей таблице. `value` — значение или функция
без аргументов; функция вычисляется **один раз** (а не для каждой строки), поэтому она нужна для
«значения этого запуска миграции» (например, времени запуска), а не для значения, своего у каждой
строки, — ссылаться на другую колонку она не может. Операция меняет только данные, поэтому
`state_forward()` ничего не делает, и откатить её по смыслу нельзя (перезаписанные `NULL` не
восстановить) — `migrate` к более ранней миграции отказывается её откатывать. `field_name` может называть
внешний ключ именем связи (`"author"`) или полем-колонкой (`"author_id"`); тогда `value` — первичный
ключ связанной строки (принимается и объект модели). Имя без одной колонки в базе даёт
`ConfigurationError`. Пакет выбирает строки по первичному ключу — по всем колонкам составного, — а у
модели без первичного ключа по собственному имени строки в базе (`ctid` в PostgreSQL, `rowid` в
SQLite); база без такого имени даёт `ConfigurationError` для модели без первичного ключа.

`AlterColumnNotNullSafe` делает колонку NOT NULL, проверив, что ни одна строка не хранит там `NULL`:
такая строка даёт `hare.exceptions.ConfigurationError` с именами модели и поля и советом использовать
`BackfillColumn` — вместо ошибки нарушения NOT NULL из базы. Как это делается:

- **PostgreSQL, миграция с `atomic = False`** — таблица никогда не сканируется под блокировкой:
  `CHECK (колонка IS NOT NULL)` добавляется `NOT VALID` (без сканирования), проверяется через
  `VALIDATE CONSTRAINT` (сканирование, которое не блокирует запись), колонке ставится NOT NULL —
  PostgreSQL берёт его из проверенного CHECK, а не из строк, — и CHECK удаляется. Если проверка нашла
  `NULL`, CHECK удаляется до ошибки.
- **PostgreSQL, атомарная миграция** — `LOCK TABLE ... IN SHARE ROW EXCLUSIVE MODE` до конца транзакции,
  проверка на `NULL`, затем `ALTER COLUMN ... SET NOT NULL`: новый `NULL` между ними не проскочит, но
  запись ждёт, пока таблица сканируется (риск [`set_not_null_scans_table`](#set-not-null-scans-table)).
- **SQLite** — проверка на `NULL`, затем таблица пересобирается с колонкой NOT NULL.
- **ClickHouse** — проверка на `NULL`, затем колонка меняется так, как её меняет `AlterField`.

## <a id="adding-a-required-column"></a>Приём 1: обязательная колонка в большой таблице

Если разбить это на три отдельные миграции, ни одна из них не будет блокировать таблицу всё время
массового заполнения:

```python
# 0004_add_status_nullable.py
class Migration(Migration):
    operations = [
        AddField("Order", "status", CharField(max_length=20, null=True)),
    ]

# 0005_backfill_status.py
class Migration(Migration):
    dependencies = [("models", "0004_add_status_nullable")]
    atomic = False
    operations = [
        BackfillColumn("Order", "status", value="pending", batch_size=5000),
    ]

# 0006_status_not_null.py
class Migration(Migration):
    dependencies = [("models", "0005_backfill_status")]
    atomic = False
    operations = [
        AlterColumnNotNullSafe("Order", "status"),
    ]
```

## <a id="renaming-a-column"></a>Приём 2: переименование колонки через запись в обе

Операции `RenameColumnSafe` нет — настоящее переименование без остановки приложения — это рецепт из 4
шагов из существующих операций (`AddField`/`RemoveField`) и `BackfillColumn`, с периодом записи в обе
колонки в коде приложения между ними, который к hare-orm не относится:

1. **Расширение** — добавьте новую колонку; старый код чтения и записи это не затрагивает:

   ```python
   operations = [AddField("Order", "customer_email", CharField(max_length=255, null=True))]
   ```

2. **Заполнение** — заполните новую колонку у существующих строк. `BackfillColumn` подходит, если
   все строки получают одно и то же значение (общее значение по умолчанию или заглушка); если новая
   колонка должна получить *собственное* значение каждой строки из старой, используйте миграцию
   данных `RunPython` с пакетным
   `UPDATE ... SET customer_email = email WHERE customer_email IS NULL` — `BackfillColumn` записывает
   одно значение на всю партию и не может ссылаться на другую колонку строки.

   ```python
   async def backfill_customer_email(apps, schema_editor):
       while True:
           rowcount, _ = await schema_editor.client.execute(
               'UPDATE "order" SET "customer_email" = "email" '
               'WHERE "id" IN (SELECT "id" FROM "order" WHERE "customer_email" IS NULL LIMIT 5000)'
           )
           if not rowcount:
               break

   operations = [RunPython(backfill_customer_email, RunPython.noop)]
   ```

3. **Запись в обе** — выпустите код приложения, который какое-то время пишет в обе колонки, чтобы и
   старый, и новый код чтения видели актуальные данные (переопределённый `Model.save()` или то же самое
   везде, где пишутся строки):

   ```python
   class Order(Model):
       email = fields.CharField(max_length=255)
       customer_email = fields.CharField(max_length=255, null=True)

       async def save(self, *args, **kwargs):
           self.customer_email = self.email
           await super().save(*args, **kwargs)
   ```

4. **Сжатие** — когда весь код чтения и записи обновлён, а все строки заполнены, удалите старую
   колонку так, как это делает [приём 5](#removing-a-field).

Каждый шаг выпускается и какое-то время работает в продакшене, прежде чем выполняется следующий, —
именно это и даёт работу без остановки, а не какой-то механизм внутри hare-orm.

## <a id="adding-an-index"></a>Приём 3: добавление индекса

`CREATE INDEX CONCURRENTLY` строит индекс, пока в таблицу пишут; внутри транзакции он выполняться не
может, поэтому миграция не атомарная:

```python
class Migration(Migration):
    dependencies = [("models", "0006_status_not_null")]
    atomic = False
    operations = [
        AddIndex("Order", Index(fields=("status",), name="order_status"), concurrently=True),
    ]
```

## <a id="adding-a-constraint"></a>Приём 4: добавление ограничения

Ограничение CHECK или внешний ключ добавляются без проверки — проверяются только новые и изменённые
строки, ничего не сканируется, — а существующие строки проверяет `ValidateConstraint` в следующей
миграции, с блокировкой, которая не мешает записи:

```python
# 0008_constraints.py
operations = [
    AddConstraint("Order", CheckConstraint(check=Q(total__gte=0), name="order_total_positive"), not_valid=True),
    AddField("Order", "coupon", ForeignKeyField("models.Coupon", null=True, db_index=False), not_valid=True),
]

# 0009_validate_constraints.py
operations = [
    ValidateConstraint("Order", "order_total_positive"),
    ValidateConstraint("Order", "fk_order_coupon_4b1c0d2a"),  # имя внешнего ключа, как его показывает база
]
```

Уникальное ограничение забирает уникальный индекс, построенный без блокировки записи, — `UNIQUE USING
INDEX` переименовывает индекс в имя ограничения и ничего заново не проверяет:

```python
class Migration(Migration):
    atomic = False
    operations = [
        AddIndex("Order", Index(fields=("number",), name="order_number_unique", unique=True), concurrently=True),
        AddConstraint(
            "Order", UniqueConstraint(fields=("number",), name="order_number_unique"), using_index="order_number_unique"
        ),
    ]
```

Модель объявляет только ограничение: `using_index` убирает индекс из `Meta.indexes` модели в состоянии
миграций.

## <a id="removing-a-field"></a>Приём 5: удаление поля или модели

Сначала поле уходит из моделей; колонка удаляется, когда её уже не читает ни один работающий код:

```python
# 0010_forget_legacy_code.py — выкладывается вместе с кодом, в котором поля уже нет
operations = [
    SeparateDatabaseAndState(state_operations=[RemoveField(model_name="Order", name="legacy_code")]),
]

# 0011_drop_legacy_code.py — после выкладки
safety_exemptions = [MigrationRiskCode.RUN_SQL]
operations = [
    RunSQL('ALTER TABLE "order" DROP COLUMN "legacy_code"', reverse_sql=RunSQL.noop),
]
```

Модель — так же: сначала `DeleteModel("Order", state_only=True)`, затем `DROP TABLE` через `RunSQL`. В
SQLite удалите колонку раньше, чем другое изменение этой таблицы её пересоберёт, — пересборка сохраняет
только колонки, которые знает состояние миграций (см. `RemoveField` в [Операциях](operations.ru.md)).
