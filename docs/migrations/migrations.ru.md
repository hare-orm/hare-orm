# Создание и применение миграций

## Команды как функции (`hare.migrations.api`) {: #migrations-api }

Каждая команда миграций — функция: CLI только разбирает аргументы, вызывает её и печатает
результат. Вызывайте их из сценария развёртывания, теста или служебного эндпоинта, не запуская
отдельный процесс:

```python
from hare.migrations.api import makemigrations, migrate, plan, sqlmigrate, squashmigrations

await makemigrations(*, config, app_labels=None, empty=False, merge=False, name=None) -> MigrationChanges
await squashmigrations(*, config, app_label, name=None) -> SquashedMigration
await migrate(*, config, app_labels=None, target=None, fake=False, dry_run=False, reporter=None, progress=None) -> None
await plan(*, config, app_labels=None, target=None) -> list[str]
await sqlmigrate(*, config, app_label, migration_name, backward=False) -> list[str]
```

`config` — всё, что принимает [`Hare.init()`](../connections/configuration.ru.md#hare-init):
`HareConfig`, словарь, путь к файлу `.json`/`.yml` или `"module.VARIABLE"`.

- `makemigrations()` возвращает миграции, которые приводят историю каждого приложения к его
  моделям, — **ещё не записанные**: в `MigrationChanges.writers` по одному `MigrationWriter` на
  новую миграцию (пусто, если ничего не изменилось), в `warnings` — возможные переименования,
  которые не удалось распознать, в `data_loss_warnings` — изменения, теряющие сохранённые значения.
  Запишите каждую через `writer.write()` (возвращает путь к файлу); `writer.as_string()` — текст
  файла, `writer.name`/`writer.app_label` её называют. Приложению без пакета миграций пакет
  создаётся. `empty=True` делает по одной пустой миграции на приложение, `merge=True` — по одной
  миграции, соединяющей разветвившуюся историю; обоим нужны `app_labels`, и вместе их задать
  нельзя. `ConfigurationError` — для неизвестного приложения, для разветвившейся истории без
  `merge` и для `merge` истории, где соединять нечего.
- `squashmigrations()` возвращает `SquashedMigration`: `writer` (одна миграция, создающая модели
  приложения с нуля; `None`, если у приложения одна миграция), `replaced_names` и
  `data_migration_names` — заменяемые миграции с `RunPython`/`RunSQL`, действие которых в сведённую
  миграцию не переносится.
- `migrate()` применяет или откатывает миграции до `target` — `"app_label"` (последняя миграция
  приложения), `"app_label.migration_name"` или `"app_label.zero"` (откатить всё); без него каждое
  приложение доходит до последней миграции. `reporter(connection_name, plan_steps, fake, dry_run)`
  вызывается с планом каждого подключения перед его выполнением, `progress(event, app_label,
  migration_name)` — вокруг каждой миграции; оба могут быть `async def`.
- `plan()` возвращает (и печатает) упорядоченные шаги, которые сделал бы `migrate()`;
  `sqlmigrate()` возвращает SQL одной миграции (с `backward=True` — её отката), не выполняя его.

```python
changes = await makemigrations(config="settings.HARE_ORM", app_labels=["models"])
for writer in changes.writers:
    print("writing", writer.write())
await migrate(config="settings.HARE_ORM")
```

`makemigrations()` и `squashmigrations()` привязывают модели в собственном контексте и к базе не
подключаются. `migrate()`, `plan()` и `sqlmigrate()` сами вызывают `Hare.init()` и оставляют
контекст инициализированным — по окончании вызовите `await Hare.close_connections()`.

## `Migration` {: #migration }

```python
class Migration:
    operations: list[Operation] = []
    dependencies: list[tuple[str, str]] = []
    run_before: list[tuple[str, str]] = []
    replaces: list[tuple[str, str]] = []
    initial: bool | None = None
    atomic: bool = True
```

При `atomic = True` (по умолчанию) вся миграция, включая её запись в таблице миграций, выполняется в
одной транзакции, если база поддерживает транзакции для команд изменения схемы. При `atomic = False`
операции выполняются по одной вне транзакции, кроме операции, объявленной с `atomic=True`
(`RunPython`/`RunSQL`): она выполняется и откатывается целиком в собственной транзакции.

SQLite применяет большинство изменений колонок, пересоздавая таблицу: по модели создаётся копия,
заполняется из старой таблицы и переименовывается на её место. Счётчик `AUTOINCREMENT` копии
продолжает счётчик старой таблицы, поэтому идентификатор удалённой последней строки больше никогда
не выдаётся; представление, ссылающееся на таблицу, продолжает работать; все триггеры и индексы
создаются заново, в том числе частичное `UniqueConstraint(condition=...)`. Если меняется тип поля,
каждое сохранённое значение преобразуется так же, как это делает `USING column::type` в PostgreSQL
(hare выполняет каждый сеанс PostgreSQL в UTC, поэтому и там так же): дата-время — в свою дату (для
значения с поясом — дату момента в UTC), дата — в свою полночь (в UTC при `use_tz=True`), число — в
логическое значение (не ноль — истина), записи `t`/`true`/`yes`/`on`/`1` (и их отрицания) — в
логическое значение, логическое значение — в текст `true`/`false`, дробное число — в ближайшее
целое, целое или число с плавающей точкой — в десятичный текст с числом знаков после запятой поля.
Значение, которое преобразование не может прочитать, копируется без изменений.

SQLite выполняет каждую миграцию с `PRAGMA foreign_keys = OFF` (это нужно для пересоздания таблиц).
Перед фиксацией атомарной миграции и после каждой операции неатомарной `PRAGMA foreign_key_check`
проверяет, что ни одна строка не ссылается на отсутствующую родительскую; нарушение даёт
`hare.exceptions.IntegrityError` со списком нарушающих строк и откатывает миграцию (или атомарную
операцию). Подключение, настроенное с выключенным `foreign_keys`, не проверяется.

Если `migrate` передано несколько целей, каждая планируется от состояния, которое оставляют
предыдущие; цели, которые одновременно применили бы и откатили одну и ту же миграцию, дают
`QueryError`.

Файлы миграций перечитываются при каждой загрузке: файл, изменённый после первого импорта, будет
учтён следующим `migrate`/`plan` в том же процессе.

## Перечисления, созданные во время работы {: #runtime-enums }

`CharEnumField`/`IntEnumField` может принимать перечисление, созданное во время работы приложения, —
например, из вариантов, заданных в панели администратора:

```python
Status = StrEnum("Status", {"NEW": "new", "DONE": "done"})
status = fields.CharEnumField(Status, default=Status.NEW)
```

У такого перечисления нет атрибута модуля, откуда его можно импортировать, поэтому миграция объявляет
его у себя в начале в той же функциональной форме, на той же основе (`StrEnum`, `IntEnum`, `IntFlag`,
`Enum`, тип данных, примешанный через `type=`) и с элементами в том же порядке — один раз, сколько бы
полей, значений по умолчанию и условий ограничений его ни использовали:

```python
from enum import StrEnum

Status = StrEnum('Status', {'NEW': 'new', 'DONE': 'done'})

class Migration(migrations.Migration):
    operations = [
        ops.AddField(model_name='Ticket', name='status',
                     field=fields.CharEnumField(default=Status.NEW, enum_type=Status, max_length=4)),
    ]
```

Перечисление импортируется, если его `__module__` и `__qualname__` ведут к самому классу или если оно
объявляет `migration_import_path`. Два разных перечисления с одним именем объявляются как `Status` и
`Status2`. Автоматическое создание миграций сравнивает перечисление по содержимому — основе, имени и
элементам по порядку, — а не по классу: то же перечисление, созданное заново после перезапуска, ничего
не меняет, а добавленный, удалённый, переименованный, изменённый или переставленный элемент даёт
`AlterField`.

## Заменяемые модели в миграциях {: #swappable-models }

Пакет пишет свои миграции один раз, а проект направляет их на свою модель через
[настройку замены](../models/relations.ru.md#swappable-models). `makemigrations` записывает связь,
объявленную через `swappable("USER_MODEL")`, этим же вызовом, а её зависимость — как
`migrations.swappable_dependency("USER_MODEL")`, а не как миграцию приложения, на которое настройка
указывает сейчас:

```python
from hare import fields, migrations
from hare.migrations import operations as ops
from hare.models import swappable


class Migration(migrations.Migration):
    dependencies = [migrations.swappable_dependency("USER_MODEL")]

    operations = [
        ops.CreateModel(
            name="Consent",
            fields=[
                ("id", fields.IntField(primary_key=True)),
                ("user", fields.ForeignKeyField(swappable("USER_MODEL"), related_name="consents", null=True)),
            ],
        ),
    ]
```

- `swappable_dependency("USER_MODEL")` — это `(app_label, "__first__")`, первая миграция приложения,
  на которое указывает настройка (для собственного приложения миграции — никакой). Она вычисляется
  при импорте файла, поэтому Hare должен быть настроен до загрузки миграций; иначе загрузка даёт
  `MigrationLoadError` с просьбой сначала вызвать `Hare.init()`.
- `CreateModel` модели с `Meta.swappable` записывает его в `options`. Пока модель заменена, каждая
  операция с её таблицей (`CreateModel`, `AddField`, `AddIndex`, ... и их откат) меняет только
  состояние миграций — `migrate` не создаёт для неё таблицу, а `migrate <app> zero` работает одинаково
  с настройкой и без неё. `sqlmigrate` и `plan` показывают внешний ключ на таблицу модели, которая
  используется на самом деле.
- Смена модели, на которую указывает настройка, ничего не меняет в миграциях пакета: поле сравнивается
  как та же ссылка. Собственная модель проекта получает обычный `CreateModel` в приложении проекта.
- `makemigrations` работает и тогда, когда у приложения проекта, на которое указывает настройка, ещё
  нет миграций: `swappable_dependency` пакета на него не записывается, пока не появится первая
  миграция этого приложения. Модель проекта, которая ссылается обратно на модель пакета, образует с
  ней цикл; `makemigrations` разбивает его на две миграции так же, как для любых двух приложений,
  переносимых вместе.
- `makemigrations` и `squashmigrations` сортируют импорты и форматируют записываемые файлы через
  ruff, если он установлен, поэтому записанная миграция проходит `ruff check`/`ruff format --check`.

Выбирайте настройку до создания таблиц пакета. `migrate` отказывается работать, пока у таблицы,
созданной применённой миграцией, есть внешний ключ `swappable()`, который ссылается не на ту таблицу,
что у модели, на которую указывает настройка сейчас, а `hare drift` сообщает о такой колонке с именем
настройки. Чтобы перевести существующий проект на другую модель, напишите свою миграцию: создайте
таблицу новой модели, скопируйте строки, перенаправьте внешние ключи таблиц пакета (`RunSQL`) и только
потом смените настройку.
