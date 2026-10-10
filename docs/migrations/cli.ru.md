# Команда `hare`

Команда `hare` (ставится как консольная программа) берёт настройки из `-c/--config` — это либо
`module.VARIABLE` (путь через точку к значению с настройками, например `settings.HARE_ORM`), либо
путь к файлу `.json`/`.yml`/`.yaml`, — а без него из переменной окружения `HARE_ORM` или из
`[tool.hare].hare_orm` в `pyproject.toml`, где лежит такое же значение. Это то, что принимает
[`HareConfig.load()`](../connections/configuration.ru.md#the-config-dict). `hare -V`/`--version`
печатает версию hare.

| Команда | Что делает |
|---|---|
| `hare init [app_labels...]` | Создаёт пакет миграций для указанных (или всех) приложений. |
| `hare shell` | Оболочка IPython (`pip install hare-orm[ipython]`) с `await` на верхнем уровне, в которой уже доступны `Hare`, `hare` (`HareContext`), `apps` и все модели — каждая ещё и как `<app_label>_<ModelName>`, что различает одноимённые модели двух приложений. |
| `hare makemigrations [app_labels...] [--empty] [--merge] [--check] [--dry-run] [-n/--name NAME]` | Создаёт миграции, сравнивая модели с их прошлым состоянием. `--empty` требует метки приложения и создаёт пустую миграцию, зависящую от текущих последних миграций. `--merge` требует метки приложения и записывает одну миграцию, соединяющую разветвившуюся историю приложения (две последние миграции); без него разветвившаяся история — ошибка. `--dry-run` печатает, что было бы записано, ничего не записывая; `--check` делает то же и завершается с кодом `1`, если есть что записать (с `--empty` оба не сочетаются). `-n`/`--name` переименовывает каждую миграцию, созданную за запуск, вместе с зависимостями между ними. Если модуль `migrations` не задан явно, приложение, модели которого лежат в `models.py` верхнего уровня (не в пакете), получает пакет `migrations` верхнего уровня рядом с ним. Два приложения, которые получили бы один и тот же пакет миграций (два таких приложения из одного файла в одном каталоге или одно и то же явное значение `migrations`), отклоняются с ошибкой любой командой миграций — задайте каждому свой модуль `migrations` (например, `"migrations_billing"`). |
| `hare squashmigrations APP_LABEL [START] END [--squashed-name NAME]` | Сжимает миграции приложения от `START` (без него — от первой) до `END` — каждая задаётся именем или однозначным началом имени — в одну, которая их заменяет: база, применившая ни одной или все, выполняет её вместо них, применившая часть — сначала выполняет оставшиеся. См. [Сжатие миграций](migrations.ru.md#squashing-migrations). |
| `hare migrate [app_label] [migration] [--fake] [--dry-run] [--lock-timeout SECONDS]` | Применяет миграции вперёд или назад — как требует текущее состояние базы: `migration` позже применённых применяет миграции до неё, раньше применённых — откатывает до неё, а `zero` откатывает все миграции приложения (`hare migrate models zero`). Без `migration` приложение доходит до последней миграции; без `app_label` — все приложения. `--fake` записывает историю, не выполняя SQL; `--dry-run` только печатает план и ничего не пишет в базу, даже в таблицу миграций. `--lock-timeout` прерывает миграцию, команда которой дольше ждёт блокировку, взятую другим сеансом (см. [Ожидание блокировок](migrations.ru.md#lock-timeout)). |
| `hare history [app_labels...]` | Список уже применённых миграций (из базы) по подключениям и приложениям. |
| `hare heads [app_labels...]` | Список последних миграций на диске (не из базы). |
| `hare inspectdb [tables...] [--connection ALIAS] [--schema SCHEMA]` | Создаёт модели hare-orm по существующей схеме базы (как `inspectdb` в Django). `--schema` по умолчанию — схема подключения по умолчанию (`current_schema()` в PostgreSQL); таблица вне её получает `Meta.schema`. |
| `hare dbshell [--connection ALIAS]` | Запускает собственный интерактивный клиент базы на подключении (по умолчанию — на первом в настройках): `psql` для PostgreSQL, `sqlite3` для файла SQLite, `clickhouse-client` для ClickHouse. Пароль передаётся клиенту через окружение (`PGPASSWORD`, `CLICKHOUSE_PASSWORD`), никогда не в аргументах; подключение с `password_provider` передаёт пароль от функции. Настройки TLS, `schema` и `application_name` PostgreSQL передаются тоже (`PGSSLMODE`, `PGSSLROOTCERT`, `PGOPTIONS`, `PGAPPNAME`). Завершается с кодом выхода клиента. База SQLite в памяти, диалект без клиента или клиент, которого нет в `PATH`, дают ошибку. |
| `hare checkmigrations [app_labels...]` | Проверяет миграции, которые применил бы `migrate`, по самой базе: операции, которые блокируют или переписывают большую таблицу или ломают код, ещё работающий во время выкладки (переименованные и удалённые колонки и таблицы, SQL как есть), — каждая печатается с тем, как сделать изменение безопасно. Завершается с кодом `1`, пока риск не перечислен в `safety_exemptions` его миграции. Таблица большая от `migrations.safety.large_table_rows` строк из конфига. `makemigrations` печатает такие же предупреждения для миграций, которые записывает, не читая базу. См. [Проверка миграций](zero-downtime.ru.md#checking-migrations). |
| `hare sqlmigrate APP_LABEL MIGRATION_NAME [--backward]` | Печатает SQL миграции, не выполняя его (для баз семейства PostgreSQL — внутри `BEGIN;`/`COMMIT;`). `--backward` печатает SQL отката. |
| `hare drift [app_labels...] [--connection ALIAS] [--schema SCHEMA]` | Сравнивает живую базу подключения (по умолчанию — первого в конфигурации) с текущими моделями указанных приложений (по умолчанию — всех), которые его используют: таблицы и колонки, которые есть в базе, но нет ни в одной миграции, и операции, нужные, чтобы привести в соответствие остальное. Завершается с кодом `1`, если что-то найдено. Модели с `Meta.managed = False` пропускаются целиком, как в `makemigrations`. `--schema` выбирает схему, в которой ищутся неизвестные таблицы (по умолчанию — текущая схема подключения). Собственные служебные таблицы hare-orm (`hare_migrations`, `hare_distributed_decisions` для `Transactions.distributed()`) никогда не считаются неизвестными. |
| `hare distributed-recover --coordinator ALIAS [--finish] [--older-than SECONDS]` | Сообщает о подготовленных транзакциях `Transactions.distributed()`, которые так и не завершились (а с `--finish` — завершает их). `--coordinator` (обязателен) — подключение, в котором хранится журнал решений `hare_distributed_decisions`. `--finish` выполняет `COMMIT PREPARED`/`ROLLBACK PREPARED` вместо одного лишь отчёта. `--older-than` пропускает всё, что моложе указанного числа секунд, потому что оно ещё может выполняться (по умолчанию 300); `0` ничего не пропускает. Завершается с кодом `1`, если найдена зависшая подготовленная транзакция (в режиме отчёта) или осталась незавершённой после `--finish`. |
| `hare stubs [--output DIRECTORY] [--relation-depth N] [--check]` | Пишет заглушки, по которым pyright и Pylance проверяют типы в модулях моделей, по умолчанию в `typings/`. `--relation-depth` (от 0 до 5, по умолчанию 2) — через сколько связей самое большее проходит ключ фильтра; `--check` ничего не пишет и завершается с кодом `1`, если заглушки нет или она устарела. См. [pyright и Pylance](../querying/type-checking.ru.md#pyright). |

```bash
hare -c settings.HARE_ORM makemigrations models -n add_user_email
hare -c settings.HARE_ORM migrate models 0003_add_index --dry-run
hare -c settings.HARE_ORM migrate models 0002_add_user_email   # назад к 0002: откатывает 0003
hare -c settings.HARE_ORM migrate models zero                  # откатывает все миграции приложения
hare -c settings.HARE_ORM sqlmigrate models 0003_add_index
hare -c settings.HARE_ORM inspectdb --connection default --schema public users orders
```

Коды завершения: `0` — успех, `2` — ошибка в вызове команды (`CLIUsageError`, выводится в stderr),
`1` — любая другая ошибка (`CLIError`).

## <a id="adding-your-own-commands"></a>Свои команды

Установленные пакеты и ваш проект могут добавлять подкоманды к `hare`, не меняя hare-orm. Команда —
это подкласс `CLICommand`:

```python
# myproject/cli.py
from hare.cli.plugins import CLICommand, CLIError, CommandContext


class ExportCommand(CLICommand):
    name = "export"                   # `hare export`
    help = "Export orders to CSV."    # показывается в `hare --help`

    def add_arguments(self, parser):
        parser.add_argument("--since")

    async def run(self, cli_context, args):
        config = CommandContext.load_config(cli_context)        # те же настройки -c/--config
        async with CommandContext.hare_cli_context(config):
            ...                                         # здесь модели и подключения готовы
        return 0                                        # код завершения; None тоже означает 0
```

`cli_context` — это `CLIContext` (`hare.cli.plugins`) с общим параметром `config` — значением
`-c`/`--config` (`module.VARIABLE` или путь к файлу), равным `None`, если оно не передано: тогда
`CommandContext.load_config(cli_context)` находит настройки через переменную окружения или
`pyproject.toml`, как любая встроенная команда.

Если `run()` бросает `CLIUsageError`, команда завершается с кодом 2, `CLIError` — с кодом 1, а
сообщение выводится в stderr, как у встроенных команд. Любое другое исключение hare-orm
(`ConfigurationError`, `DBConnectionError` и т. п.) тоже завершает с кодом 1 и печатает тип и
сообщение вместо полного стека вызовов; так же поступает встроенная команда, настройки которой не
удаётся загрузить (неизвестный `engine`, неверный порт и т. п.). Модуль с командой импортируется при
каждом запуске `hare`, поэтому держите его лёгким, а тяжёлые зависимости импортируйте внутри `run()`.

Команду можно зарегистрировать двумя способами:

- **Установленный пакет** объявляет точку входа в группе `hare.cli` своего `pyproject.toml`; команда
  появляется, как только пакет установлен:

  ```toml
  [project.entry-points."hare.cli"]
  export = "myproject.cli:ExportCommand"
  ```

- **Сам проект** перечисляет команды в своих настройках рядом с `connections` и `apps`
  (`Hare.init()` этот раздел не читает — его читает только CLI):

  ```python
  HARE_ORM = {
      "connections": {...},
      "apps": {...},
      "cli": {"commands": ["myproject.cli:ExportCommand"]},
  }
  ```

Команда с уже занятым именем — встроенной командой или раньше подключённым модулем — пропускается с
предупреждением, в котором названы оба источника; встроенные команды всегда важнее. Модуль, который
не удалось импортировать или который не смог объявить свои аргументы, тоже пропускается с
предупреждением, а все остальные команды продолжают работать.
