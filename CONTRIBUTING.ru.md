# Участие в разработке hare-orm

[English](CONTRIBUTING.md)

Спасибо за помощь. Приветствуются сообщения об ошибках, исправления документации, новые
возможности и новые диалекты.

## Как изменение попадает в проект

- **Ветки.** Разработка идёт в ветке `dev`, и каждый пул-реквест направляется в неё. `main`
  указывает на последний выпуск и сдвигается только при выпуске (см. [Выпуски](#выпуски)).
- **Крупное сначала обсуждается.** Новый публичный API, изменение существующего поведения или новая
  возможность диалекта начинаются с issue, чтобы подход был согласован с мейнтейнером до того, как
  написан код. Исправление ошибки или документации можно сразу присылать пул-реквестом.
- **Ревью и одобрение.** Каждый пул-реквест проверяет мейнтейнер (GitHub запрашивает ревью
  автоматически через `.github/CODEOWNERS`). Он принимается, только когда мейнтейнер его одобрил,
  CI зелёный (проверки и та [матрица тестов](#матрица-тестов-и-ci), которую CI для него запускает) и у каждого коммита есть
  подпись (см. [Подпись](#подпись)). Мейнтейнер может отклонить изменение, которое не подходит
  направлению проекта, и объясняет почему.
- **Правило устройства.** Изменение того, как hare-orm выглядит или ведёт себя, должно быть
  настоящим улучшением. Там, где это дело вкуса, hare-orm делает так же, как ORM Django, чтобы
  пользователи Django оказывались на знакомой почве.
- **Никаких прослоек совместимости до 1.0.** Переименовывая или убирая публичный API, поменяйте все
  его использования и опишите изменение в разделе *Breaking changes* файла
  [CHANGELOG](CHANGELOG.ru.md). Старое имя не оставляйте псевдонимом (см.
  [Версии и выпуски](README.ru.md#версии-и-выпуски)).

Мейнтейнер: Владислав Яременко ([@VladislavYar](https://github.com/VladislavYar)).

## Подготовка окружения

Нужны Python 3.12, 3.13 или 3.14 и [Poetry](https://python-poetry.org/).

```bash
make deps
```

(выполняет `poetry install --extras asyncpg --extras opentelemetry`). Заодно ставятся группы
зависимостей для разработки, тестов, contrib и документации — отдельно их ставить не нужно.

Один раз установите git-хуки, чтобы ошибки стиля и типов ловились до CI:

```bash
poetry run pre-commit install
```

При каждом коммите хуки выполняют `codespell`, `check-yaml`, `end-of-file-fixer` и `trailing-whitespace`,
затем `make _check` — те же проверки ruff, mypy, bandit, Rust, actionlint и документации тех же версий, что и
`make check` и задание `check` в CI, — а для коммита, затрагивающего файлы Python, ещё и `make test`
(весь набор SQLite). Проверкам Rust нужен инструментарий Rust, а
actionlint запускается в Docker (см. [Стиль кода и проверки](#стиль-кода-и-проверки)).

## Устройство проекта

Один пакет на область, одно понятие на модуль — имя модуля говорит, какой класс или какую задачу
он содержит. Публичный API — то, что экспортируют `hare`, `hare.fields`, `hare.models`,
`hare.query.expressions`, `hare.query.functions` и описанные в документации модули; всё остальное
может переехать.

| Пакет | Что в нём |
| --- | --- |
| `hare/` | Публичные точки входа (`__init__.py`), `exceptions.py` и `warnings.py` — больше ничего. |
| `hare/core/` | Запуск и хранение ORM: `Hare` (`hare.py`), его конфигурация (`config/`), контекст, в котором работает процесс или тест (`hare_context.py`), подключения (`connections/`) и маршрутизатор (`routing/`), реестр приложений и моделей (`apps/`: реестр, связывание связей, заменяемые модели, поиск моделей, проверки имён таблиц), кэши и их сброс (`caching/`) и `Registries` — то, что регистрируют диалекты, поля и пакеты. |
| `hare/models/` | `Model` — его публичный API — и классы, выполняющие его шаги: подготовка экземпляра, подключения, копии, сохранение, проверки и изменённые поля (`instances/`), запись строк (`write/`: писатель, вставки, обновления, откат значений, захват изменений), удаление и мягкое удаление с каскадами и предпросмотром (`deletion/`), построение класса и проверка его объявления (`class_building/`), `MetaInfo` и метакласс (`model_meta.py`), арендаторы (`tenancy/`). |
| `hare/fields/` | `Field` (`field.py`), поля значений по семействам (`data/`: `numeric`, `text`, `temporal`, `json`, `choices`, `containers` — массивы, словари, кортежи, — `network`, логические, двоичные поля и UUID), поля связей, их доступ и связанные менеджеры (`relations/`), генерируемые, шифрованные и заменяемые поля, умолчания базы (`db_defaults/`), зарегистрированные операторы фильтров и преобразования (`registrations/`), валидаторы (`validators/`: ограничения и форматы). |
| `hare/query/` | Построение запросов: `QuerySet` и классы проверки его аргументов (`queryset/`), команды, которые выполняет queryset (`statements/`: построение — соединения, группировка, сортировка, keyset, блокировки строк, аннотации, условия, CTE; `select/` — values, строки моделей, операции над множествами; `summary/`; `write/` — обновление, удаление, массовые записи, merge), планы команд и то, как части запроса их описывают (`plans/`), выражения (`expressions/`), функции ORM (`functions/`), операторы фильтров и фильтры (`filters/`), менеджеры, области видимости, загрузка связей, пути и API описания фильтров (`lookup_info/`). |
| `hare/sql/` | Построитель SQL, через который выводит ORM: термины, условия, функции и построители запросов (`builder/`) — без моделей и подключений. |
| `hare/ddl/` | Объекты схемы, объявляемые в `Meta`: индексы, ограничения, триггеры, параметры таблиц, выражения, которые объявление пишет в DDL (`expressions/`), и нейтральное к диалекту экранирование имён и литералов. |
| `hare/dialects/` | По пакету на базу (`sqlite/`, `postgresql/` с `drivers/asyncpg` и `drivers/rust_pg`, `clickhouse/` с `drivers/clickhouse_connect` и `drivers/clickhouse_driver`) на контракте из `base/`: `Dialect` и `Features`, подключение и его клиент (`connection/`, `client/`), транзакции, литералы, параметры, части команд, рендереры, типы, операторы фильтров, полнотекстовый поиск (`search/`), редакторы схемы и их части (`schema/`), правила проверки миграций. То, что несколько диалектов hare пишут одинаково сверх ISO SQL, — тоже класс `base/`, названный по тому, что он содержит (`LimitReturningConflictQueryClauses`). Ядро не импортирует ни один диалект; класс конкретного диалекта живёт в своём диалекте. |
| `hare/gis/`, `hare/search/`, `hare/vectors/` | Геоданные, полнотекстовый поиск и поиск по векторам — один API на каждом диалекте, где они есть; SQL пишет каждый диалект. |
| `hare/transactions/` | `Transactions` (`atomic()`, `on_commit()`), их параметры и двухфазные транзакции между базами. |
| `hare/migrations/` | Файлы и операции миграций (`migration.py`, `operations/`), состояние проекта (`state/`), чтение миграций (`loading/`), их выполнение (`execution/`), поиск операций изменения (`autodetection/`), построение и сжатие новых миграций (`making/`) и их запись (`writer/`), проверки безопасности (`safety/`), расхождение схемы (`drift/`) и программный API (`api/`). |
| `hare/instrumentation/` | Наблюдатели и их события, обёртки и теги запросов, события изменения строк (`change_events.py`) и захват изменений модели (`capture/`). |
| `hare/inspectdb/` | Чтение существующей схемы базы в определения моделей. |
| `hare/cli/` | Команда `hare`. |
| `hare/contrib/` | Необязательные интеграции: плагины фреймворков, модели pydantic, запросы из параметров HTTP-запроса, помощники для тестов, транзакционный outbox, notify, taskiq, OpenTelemetry. |
| `hare/time/` | Часовые пояса и системные часы, по которым hare ставит каждую отметку времени. |
| `hare/health/` | Проверки здоровья подключений и критерии, по которым их оценивают. |
| `hare/stubs/` | Заглушки модулей моделей для pyright — команда `hare stubs`. |
| `hare/typing_info/` | Что знают о моделях инструменты типизации — плагин mypy и `hare stubs`. |
| `hare/classes/` | Пути классов, которые пишет и читает миграция, свойства классов, объявленные подклассы. |
| `hare/lazy_loading/` | То, что hare готовит при первом использовании, а не при импорте: регулярные выражения, классы pydantic. |
| `hare/native/` | Части скомпилированного расширения hare `rust.native`, которыми ускоряется код на Python. |
| `hare/numbers/` | Отличие конечного числа от всего остального, чем может оказаться параметр или аргумент. |

Куда класть новый код: константу — в `constants.py` пакета, который её использует, перечисление —
в его `enums.py`; вспомогательная функция, относящаяся к классу, — это метод этого класса, а не
функция рядом с ним; для нового понятия — свой модуль, а не новый класс в постороннем модуле.

Любой кэш на весь процесс регистрируется в `hare.core.caching.caches.Caches` там, где он объявлен, чтобы
изменение модели или реестра его сбрасывало, — никогда `functools.cache`/`lru_cache` или словарь на
уровне модуля: `Cache(max_size)` — для значений по ключу (ключ с классом модели хранится и
сбрасывается вместе с этой моделью), `ModelCache()` — для одного значения на класс модели
(`ModelCache(depends_on_other_models=True)`, если значение описывает и другие модели), или
`Caches.register(cache)` — для всего остального, у чего есть `forget_model(model)` и
`forget_all()`. `tests/model_setup/binding/test_model_caches.py` падает, если вместо них объявлен `WeakKeyDictionary` с
моделями в качестве ключей.

Новая часть запроса объявляет, как она входит в план, а не описывает его вручную: выражение или
оконная функция перечисляет в `plan_parts`, как каждый атрибут входит в план
(`hare.query.plans.enums.PlanPartType`), класс запроса — в `plan_slots`, как каждая настройка входит в
ключ (`PlanKeyForm`); описание генерируется из объявления, когда класс создаётся. Построение разрешает
каждый аргумент через `ExpressionArguments.get_result()`, который записывает ссылку подставляемого значения
под атрибутом, где оно лежит: план подставляет значения по их происхождению, а не по порядку записи.
Класс, который не хранит план, объявляет `plannable = False`; класс, который не делает ни того ни
другого, падает уже при определении, а `--verify-plans` падает на атрибуте, который не объявлен ни
одной частью. Ключи планов никогда не собираются обходом частей запроса снаружи.

## Запуск тестов

Набор тестов целиком выполняется на четырёх базах. Тест, которому нужно то, чего нет у диалекта
(транзакции, внешние ключи, расширение PostgreSQL), пропускается там по признаку возможности, а не
по имени диалекта.

```bash
make test            # весь набор на SQLite
make test_sqlite     # то же без тестов, не читающих тестовую базу
```

Часть тестов вообще не читает тестовую базу: прогоны плагина mypy, стабы, проверки исходного кода
hare, программы, запускаемые в отдельном процессе. Они помечены `database_independent` и образуют
отдельный набор, который не входит в набор ни одной базы, — поэтому выполняются один раз, а не
заново для SQLite и каждого драйвера PostgreSQL:

```bash
make test_database_independent
```

Новый модуль такого рода помечается `pytestmark = pytest.mark.database_independent`. `make test` и
обычный запуск `pytest` выполняют все тесты, включая эти, - кроме тестов `timing`, описанных ниже.

Тест ждёт того, что делает фоновая задача, по самому событию - событию, состоянию пула, пришедшему
сигналу (`tests/utils/waiting.py`: `wait_until`), - и никогда по задержке: его исход не должен
зависеть от скорости машины. Тест, предмет которого само время - истекающие таймаут, аренда, период
обновления, - помечается `@pytest.mark.timing` и в прогоны не входит, пока его не попросили:

```bash
make test_timing                       # тесты времени на SQLite
HARE_TEST_DB=... make test_timing      # на PostgreSQL
HARE_TEST_DB=clickhouse+... pytest --run-timing -m timing tests/dialects/clickhouse   # тесты ClickHouse
pytest --run-timing                    # все тесты вместе с тестами времени
```

Тестам PostgreSQL нужен запущенный сервер. `make test_db_up` собирает и запускает тестовый сервер
(`tests/docker/postgres`: PostGIS, pgvector и расширения contrib, так что ни один тест только для
PostgreSQL не пропускается) на `localhost:5433` с выключенной надёжностью записи: набор создаёт и
удаляет сотни баз, а каждый `DROP DATABASE` вызывает контрольную точку.

```bash
make test_db_up                 # один раз; `make test_db_down` его удаляет
make test_postgres_asyncpg      # весь набор через драйвер asyncpg на чистом Python
make test_postgres_rust         # весь набор через драйвер на Rust (сначала его нужно собрать, см. ниже)
```

Не направляйте набор тестов на сервер с данными, которые вам дороги. Чтобы взять другой сервер,
переопределите параметры подключения в командной строке (`HARE_POSTGRES_WORKERS` задаёт число
процессов xdist, по умолчанию 8):

```bash
make test_postgres_asyncpg HARE_POSTGRES_HOST=db.local HARE_POSTGRES_PORT=5432
```

Ещё трём прогонам нужны свои серверы; каждый запускается в Docker своей целью `*_up` и удаляется
целью `*_down`:

```bash
make test_db_up test_pgbouncer_up && make test_pgbouncer   # набор PostgreSQL через PgBouncer (порт 6432), оба драйвера
make deps options="--extras clickhouse-connect --extras clickhouse-driver"   # оба драйвера ClickHouse, для следующей строки
make test_clickhouse_up && make test_clickhouse            # тесты диалекта ClickHouse на обоих драйверах, ClickHouse 25.8 на портах 8124 (HTTP) и 9124 (родной протокол)
make test_brokers_up && make test_brokers                  # outbox на Redis (6390), RabbitMQ (5673), Kafka (9095)
```

PgBouncer работает в режиме пула транзакций с подготовленными командами между транзакциями (1.22.1+,
самая старая версия, которую поддерживает hare); то, чему нужен свой сеанс (`LISTEN`, тайм-аут
блокировки сеанса), идёт напрямую на сервер. Для ClickHouse выполняются только собственные тесты
диалекта (`tests/dialects/clickhouse`): общим тестовым моделям остального набора нужны ограничения
уникальности и внешние ключи, которых ClickHouse не хранит. Без своих серверов тесты брокеров пропускаются.

Каждая цель `make` и каждый прогон матрицы передают `--verify-plans` (`tests/plan_verification`):
каждый запрос, выполненный по закэшированному плану — плану команды или плану по сигнатуре вызовов, —
строится заново без планов, и его SQL и параметры сравниваются с планом. Расхождение роняет тест с
обеими командами и ключом плана, так что план, забывший часть запроса, ловит тот тест, который его
выполнил. Передавайте опцию и своему запуску `pytest`:

```bash
HARE_TEST_DB=sqlite+aiosqlite://:memory: poetry run pytest --verify-plans tests/queries/querysets/test_queryset.py
```

`make testall`/`make ci` выполняет `check`, а также `test_database_independent`, `test_sqlite` и
`test_postgres_asyncpg` — всё, кроме `test_postgres_rust`, которому нужен инструментарий Rust. CI
выполняет набор на версиях Python, SQLite и PostgreSQL [матрицы тестов](#матрица-тестов-и-ci),
через оба драйвера PostgreSQL — на всех, при каждом запуске.

## Сборка расширений на Rust

Для `test_postgres_rust` и любой локальной работы с `rust/` нужны [rustup](https://rustup.rs/) и
[maturin](https://www.maturin.rs/). `rust-toolchain.toml` закрепляет версию Rust, которую использует и
CI, — rustup ставит её при первом вызове `cargo` в checkout, поэтому проверка, прошедшая у вас, проходит и
в CI:

```bash
make build_native           # rust.native для текущей ОС и текущего Python, в rust/
make build_native_linux     # rust.native для Linux и Python 3.12-3.14, в Docker
make build_native_windows   # rust.native для Windows и Python 3.12-3.14 (.venv-3.X-core)
make build_native_macos     # rust.native для macOS и Python 3.12-3.14 (.venv-3.X-core), universal2
make rust_check             # cargo fmt --check, clippy и модульные тесты Rust (входит в make check)
```

`rust.native` (`rust/native/`) содержит драйвер PostgreSQL для адресов `postgresql://` (`rust.native.pg`)
и чтение и запись строк с кодеками полей (`rust.native.rows`). Во время работы он необязателен: без него
используйте адреса `postgresql+asyncpg://`, а строки читаются и пишутся на чистом Python. CI собирает его
при каждом запуске для Python 3.12, 3.13 и 3.14 на Linux, Windows и macOS (под macOS — один файл
universal2 на версию Python для Apple silicon и Intel) и передаёт всем заданиям, которым он нужен.
Собранные файлы не коммитятся: в checkout `rust.native` нет, пока его не соберёт `make build_native`
инструментарием Rust, который закрепляет `rust-toolchain.toml`; колёса на PyPI несут его собранным.

## Матрица тестов и CI

Матрица тестов — все сочетания, на которых выполняется набор тестов:

| Ось | Версии |
| --- | --- |
| Python | 3.12, 3.13, 3.14 |
| SQLite | 3.35.5 (самая старая поддерживаемая), 3.37.2 (собственная в Ubuntu 22.04; таблицы `STRICT` с 3.37), 3.38.0 (`->>`), 3.41.0 (`unhex()`) и 3.53.4 (новейшая), а также новейшая с установленными функциями регулярных выражений SQLite; на Linux, Windows и macOS |
| PostgreSQL | 14, 15, 16, 17, 18 — каждая через `asyncpg` (`postgresql+asyncpg://`) и через драйвер на Rust (`postgresql://`); на Linux |
| PgBouncer | 1.22, 1.23, 1.24, 1.25, 1.26 — каждая перед PostgreSQL 18, 1.26 ещё и перед 14, через оба драйвера PostgreSQL; на Linux |
| ClickHouse | 25.8, 25.10, 25.12, 26.3, 26.7, 26.8, 26.9 — каждая сервером и кластером из двух серверов, через `clickhouse-connect` (HTTP) и `clickhouse-driver` (родной протокол); на Linux |
| Redis, RabbitMQ, Kafka | самая старая версия, которую поддерживает hare, и новейшая: Redis 7.2 и 8.10, RabbitMQ 4.0 и 4.3, Kafka 3.9 и 4.3; на Linux |

Каждая версия каждой оси проверяется хотя бы раз, но не с каждой версией каждой другой оси: CI
проверяет все версии SQLite и PostgreSQL на каждом Python, а PgBouncer, ClickHouse и брокеры — на
новейшем Python (какие сочетания выполняются, видно в таблице [CI](#ci)). Версии перечислены в
`tests/matrix/constants.py` и в переменных `matrix_*` Makefile.

Версии SQLite перечислены в `tests/sqlite_versions/constants.py`. Прогон на одной из них загружает
именно эту версию, с какой бы SQLite ни был собран Python:

- **Linux**: `libsqlite3.so.0`, собранная из amalgamation этой версии и найденная через
  `LD_LIBRARY_PATH`;
- **Windows**: официальная `sqlite3.dll` этой версии рядом с копией собственного `_sqlite3.pyd`
  Python, первой в `PYTHONPATH`;
- **macOS**: `libsqlite3.0.dylib`, собранная из amalgamation, через `DYLD_LIBRARY_PATH`. В сборках
  python.org (их ставит `actions/setup-python`) SQLite вкомпонована в `_sqlite3` статически, и путь
  поиска библиотек там ничего не меняет: тогда модуль `_sqlite3` собирается из исходников CPython
  той версии Python, что запущена (скачиваются с python.org), с собранной библиотекой и ставится
  первым в `PYTHONPATH`. То же происходит на Linux для Python, чей `_sqlite3` находит SQLite через
  собственный `RPATH` (сборки manylinux).

Подготовка версии проверяет новым интерпретатором, что `sqlite3.sqlite_version` равна запрошенной,
и иначе падает; кроме того, каждый прогон тестов получает `HARE_TEST_SQLITE_VERSION` и
останавливается до первого теста, если загружена другая SQLite, — прогон никогда молча не
переходит на системную SQLite.

### Окружения тестов

Каждый тест идёт в одном окружении `SUITE_ENVIRONMENTS` из `tests/matrix/constants.py` — в том, что ставит
ровно нужные ему пакеты: `core` не ставит ни одного extra, `litestar` — extra `litestar`, `pure` — ничего
необязательного и без `rust.native` (чистый wheel). Код, который ломается без пакета, ловят окружения без
него, а тест никогда не пропускается из-за того, что пакета нет.

Матрица гоняет окружения extras на тестах, которые не читают базу, — один раз на Python и ОС; каждое
собирает только модули, где могут быть его тесты (`tests/matrix/extra_test_modules.py`): модуль, чей
`REQUIRES`, `conftest.py` над ним или то, что он импортирует из набора тестов, называет строкой один из extras
окружения; окружение, тестов которого не может быть ни в одном модуле, не запускается. На базе
каждый тест идёт в `all`, где стоят все extras, на каждой версии сервера — один прогон на версию и
драйвер; `core` и `pure` идут там только на новейшем сервере каждой базы и новейшем Python.

- Модуль тестов, которому нужен extra, объявляет это перед остальным кодом: `REQUIRES = ("litestar",)` —
  читается без импорта модуля, поэтому прогон без пакета его и не импортирует. `conftest.py` объявляет так
  требование для всего каталога.
- Отдельный тест или параметр добавляет своё через `@pytest.mark.requires("encryption")` или
  `pytest.param(..., marks=pytest.mark.requires("yaml"))` и импортирует пакет внутри теста.
- `native` — требование теста, которому нужен `rust.native`: такой тест не идёт в `pure`.
- Тест, требующий того, что ни одно окружение не ставит в точности, роняет сбор: добавьте окружение.
- Тест, параметризованный драйверами PostgreSQL, берёт их классы по пути
  (`DriverClasses.get_parameters(*POSTGRES_CLIENT_PATHS)`, фикстура `client_class` с `indirect=True`), и
  параметр asyncpg идёт только там, где asyncpg установлен.

`pytest --hare-suite-environment=<имя>` выполняет тесты одного окружения; без параметра идут тесты всех
окружений (`all`), для этого нужно виртуальное окружение со всем — `make deps`. Матрица выполняет каждое
окружение в своём виртуальном окружении `.venv-<python>-<окружение>[-<extra драйвера>]`; для прогона на базе
к нему добавляется extra её драйвера:

```bash
make matrix_venvs MATRIX_OPTIONS="--python 3.14 --environment core --environment litestar"
make test_environment ENVIRONMENT=litestar HARE_TEST_DB=postgresql://postgres:postgres@127.0.0.1:5433/test_{}
```

### Контрактные матрицы

`tests/contracts/` проверяет каждую возможность hare против каждого места, где она используется, — матрицей:
строки перебираются из самого hare (`HareRegistry` обходит его пакеты), поэтому класс, добавленный в hare,
проверяется сразу, а строка без ожидаемого результата валит тест полноты, а не пропускается.

| Матрица | Строки × столбцы | Где |
| --- | --- | --- |
| Поле × место значения | каждый конкретный класс поля (`field_samples.py`) × миграция, событие изменения (строгий JSON), схема pydantic, запрос из параметров, фабрика, inspectdb | `test_field_*.py` |
| Путь записи × наблюдатель | каждый метод, пишущий строки (`writes/write_methods.py`), × `ChangeCapture`, `RowsChanged`, валидаторы, `auto_now`, оптимистическая блокировка, область арендатора, шифрование, `GeneratedField` | `writes/` |
| Extra × точка входа | каждый модуль, импортируемый в окружении, реестр диалектов, CLI, URL драйвера без его пакета | `entry_points/` |
| Модель contrib × миграции | модели outbox, версионные, с арендатором, с шифрованием, gis, векторы, общий и составной ключ, many-to-many | `contrib_models/` |
| Интеграция × фикстура тестов | Robyn, Litestar, FastAPI, taskiq × `hare_app_db`, `hare_app_transactional_db` × драйвер | `applications/` |
| Документация × код | каждый `from hare… import …` в `docs/**/*.md` | `documentation/` |
| Опция `Meta` × наследование | каждая опция модели и опция расширения × без `Meta`, пустой, `Meta(Base.Meta)`, переопределённая, `None` | `meta_options/` |
| Исключение × ответ интеграции | каждое публичное исключение × статус каждой интеграции, без текста базы | `exceptions/` |

Новому классу поля нужен свой `FieldSample` (значения и их JSON-формы), новому методу записи — путь записи,
новой опции `Meta` — образец, либо запись с причиной в `tests/contracts/constants.py`. Ячейка, падающая
из-за hare, — ошибка, которую чинят, а не ожидание, которое правят. Матрицы идут в каждом окружении и на
каждой базе матрицы (`MATRIX_CLICKHOUSE_TEST_PATHS` их включает). Тестовые URL ClickHouse называют Keeper
сервера (`keeper_hosts`): серии `generateSerialID` тестовой базы удаляются вместе с ней, и локальные прогоны не
доходят до `max_autoincrement_series` сервера (1000).

### Матрица на своей машине

Нужны Python 3.12, 3.13 и 3.14 (`py -3.X` на Windows, `python3.X` на остальных системах), Docker для
серверов, а на Linux и macOS — компилятор C для библиотек SQLite. Прогоны rust_pg берут
сборки `rust.native` из `rust/`: файлы для Linux, Windows и macOS из репозитория или
`make build_native_linux`/`make build_native_windows`/`make build_native_macos` после изменений в
`rust/`.

```bash
make matrix_venvs         # виртуальное окружение каждого Python, окружения и драйвера базы прогонов
make sqlite_versions      # все версии SQLite, подготовленные для каждого из этих Python
make test_db_matrix_up    # PostgreSQL 14-18: hare-orm-test-db-14 ... -18 на портах 5414-5418
make test_pgbouncer_matrix_up          # PgBouncer 1.22-1.26 перед PostgreSQL 18, 1.26 ещё и перед 14
make test_clickhouse_matrix_up         # сервер ClickHouse на версию: HTTP 3YYMM, родной протокол 4YYMM, Keeper 6YYMM
make test_clickhouse_cluster_matrix_up # кластер ClickHouse из двух серверов на версию: HTTP 5YYMM, родной протокол 2YYMM
make test_brokers_matrix_up            # Redis 7.2/8.10 (16XYY), RabbitMQ 4.0/4.3 (15XYY), Kafka 3.9/4.3 (19XYY)
make test_matrix          # все прогоны, затем таблица: Python x окружение x набор -> прошёл/упал, счётчики, время
make test_db_matrix_down test_pgbouncer_matrix_down test_clickhouse_matrix_down \
    test_clickhouse_cluster_matrix_down test_brokers_matrix_down   # удаляет серверы
```

Порт сервера составлен из цифр его версии (`tests/matrix/matrix_ports.py`): `32508` — HTTP ClickHouse
25.8, `12218` — PgBouncer 1.22 перед PostgreSQL 18. Запускайте только серверы тех наборов, которые
выполняете: все серверы и кластеры ClickHouse сразу требуют много памяти.

Прогон — это тесты одного окружения в одном наборе. Каждый прогон пишет вывод pytest в
`.test-matrix/<python>-<окружение>-<набор>.log`; `make test_matrix` падает, если упал хотя бы один
прогон; прогон, в котором у окружения нет тестов, проходит. Наборы — `database-independent`, `sqlite-<версия>`,
`sqlite-regexp`, `asyncpg-<postgres>`, `rust_pg-<postgres>`, `pgbouncer-<asyncpg|rust_pg>-<pgbouncer>-<postgres>`,
`clickhouse-connect-<clickhouse>`, `clickhouse-driver-<clickhouse>`, `clickhouse-connect-cluster-<clickhouse>`,
`clickhouse-driver-cluster-<clickhouse>`, `redis-<версия>`, `rabbitmq-<версия>` и `kafka-<версия>`. Часть
матрицы выполняется с параметрами
`tests/matrix/matrix_runner.py`, а подготовительные цели сужаются своими переменными:

```bash
make test_matrix MATRIX_OPTIONS="--python 3.13 --suite 'sqlite-*' --suite rust_pg-16"
make test_matrix MATRIX_OPTIONS="--workers auto --print-failed-logs"   # по умолчанию 8 процессов
make test_matrix MATRIX_OPTIONS="--jobs 4 --workers 4 --suite 'asyncpg-*'"   # четыре прогона сразу, по 4 процесса
make test_matrix MATRIX_OPTIONS="--newest"   # только новейший Python и новейшая версия каждого сервера
make test_matrix MATRIX_OPTIONS="--python 3.14 --suite 'clickhouse-*-26.3'"   # ClickHouse 26.3, сервер и кластер
make test_matrix MATRIX_OPTIONS="--environment core --environment 'clickhouse-*'"   # тесты этих окружений
make sqlite_versions matrix_sqlite_versions=newest
make matrix_venvs MATRIX_OPTIONS="--python 3.13" matrix_interpreter=/usr/bin/python3.13
make sqlite_versions matrix_python_versions=3.13 matrix_sqlite_versions=3.35.5
make test_db_matrix_up matrix_postgres_versions=16
make test_clickhouse_matrix_up test_clickhouse_cluster_matrix_up matrix_clickhouse_versions=26.3
make test_pgbouncer_matrix_up matrix_pgbouncer_versions=1.22 matrix_postgres_versions=18
```

`--suite` — имя набора или шаблон в стиле оболочки; шаблон, которому не соответствует ни один
набор, — ошибка.

Тесты SpatiaLite (`tests/dialects/sqlite/test_spatialite.py`) загружают библиотеку, которую называет
`HARE_TEST_SPATIALITE_PATH`, — имя, которое находит загрузчик системы, или путь, — и указывают PROJ на
`HARE_TEST_SPATIALITE_PROJ_DATABASE`, если она задана; без библиотеки они пропускаются. В Linux
установите `libsqlite3-mod-spatialite` и задайте `HARE_TEST_SPATIALITE_PATH=mod_spatialite`; в macOS —
`brew install libspatialite` и `HARE_TEST_SPATIALITE_PATH=$(brew --prefix)/lib/mod_spatialite`; в
Windows `make spatialite` скачивает официальную сборку в `tests/spatialite/` и печатает обе переменные.
CI задаёт их в заданиях `test-sqlite`.

### Нижние версии зависимостей

Прямые зависимости hare — зависимости `[project]` из `pyproject.toml` и все дополнительные пакеты —
проверяются и на самых низких версиях, которые допускают их объявленные границы, на каждом Python.
Нижняя граница — это обещание: если hare нужно больше, граница в `pyproject.toml` поднимается.

```bash
make test_db_up                 # тестовый сервер на 5433
make lowest_dependencies_venv   # .venv-lowest на Python 3.12 (LOWEST_DEPENDENCIES_PYTHON=3.13 — на другом, LOWEST_DEPENDENCIES_VENV=<путь> — в другом месте)
make test_lowest_dependencies   # набор на SQLite и на PostgreSQL через оба драйвера
```

`lowest_dependencies_venv` нужен [uv](https://docs.astral.sh/uv/): зависимости разрешаются через
`uv pip compile --resolution lowest-direct` в `.venv-lowest/requirements.txt` и ставятся оттуда.
Зависимость, которую другая требует в более новой версии, получает эту более новую версию (Robyn
требует `orjson` 3.11.5 и `uvloop` 0.22, FastAPI — `pydantic` 2.7), — в файле видно, что
поставлено на самом деле. Инструменты тестов (`pytest` с плагинами, `pytz`, `ruff` — он форматирует файлы миграций, которые читают тесты) не зависимости hare и
остаются на версиях из `poetry.lock` (`tests/lowest_dependencies/locked_tool_requirements.py`).

### CI

CI (`.github/workflows/ci.yml`) запускается для каждого пул-реквеста, каждую неделю, по запросу
(*Run workflow*) и перед выпуском (его вызывает процесс `release`). Каждый запуск проверяет всю
матрицу, кроме пул-реквеста из `dev` в `main`: её прошёл каждый пул-реквест в `dev`, и выпуск снова
проверяет её на теге. Пуш в `dev` или `main` тестов не запускает — сжатый коммит и есть дерево,
которое проверил его пул-реквест: правила веток требуют статус `ci-passed` на ветке, не отстающей от
базовой. Пуш в `main` публикует документацию (`.github/workflows/docs.yml`, `make docs`).

Каждое задание выполняет цели Makefile, описанные выше, — задания тестов вместе и есть
`make test_matrix`, каждое — его часть: тесты каждого [окружения](#окружения-тестов) в его наборах, в
собственном виртуальном окружении этого окружения:

| Задание | Что делает | Цели Makefile |
| --- | --- | --- |
| `check` | проверка форматирования ruff, ruff, mypy и bandit на Python 3.12; cargo fmt, clippy и модульные тесты Rust; actionlint; строгая сборка документации | `make deps`, `make check` |
| `build-native-linux` | `rust.native` для Python 3.12, 3.13 и 3.14 под Linux, в образе maturin (manylinux2014) | `make build_native_linux` |
| `build-native` | `rust.native` для Python 3.12, 3.13 и 3.14 под Windows и под macOS (universal2), по заданию на систему | `make deps`, `make build_native_windows`, `make build_native_macos` |
| `test-sqlite` | все версии SQLite и `sqlite-regexp`, на Linux, по заданию на версию Python | `make matrix_venvs`, `make sqlite_versions`, `make test_matrix` |
| `test-database-independent` | тесты, не читающие тестовую базу (`database-independent`), по заданию на ОС (Linux, Windows, macOS) и версию Python | `make matrix_venvs`, `make test_matrix` |
| `test-postgres` | одна версия PostgreSQL через оба драйвера, по заданию на версию PostgreSQL и Python | `make test_db_matrix_up`, `make matrix_venvs`, `make test_matrix` |
| `test-lowest-dependencies` | нижние версии зависимостей, по заданию на версию Python | `make test_db_up`, `make lowest_dependencies_venv`, `make test_lowest_dependencies` |
| `test-pgbouncer` | набор PostgreSQL через один PgBouncer перед одним PostgreSQL, оба драйвера, на Python 3.14: каждый PgBouncer перед PostgreSQL 18, 1.26 ещё и перед 14 | `make test_db_matrix_up`, `make test_pgbouncer_matrix_up`, `make matrix_venvs`, `make test_matrix` |
| `test-clickhouse` | тесты диалекта ClickHouse на одной версии ClickHouse и на кластере из неё, оба драйвера: каждая версия на Python 3.14, 25.8 ещё и на 3.12 и 3.13 | `make test_clickhouse_matrix_up`, `make test_clickhouse_cluster_matrix_up`, `make matrix_venvs`, `make test_matrix` |
| `test-brokers` | обработчики пробуждения и доставки outbox на самых старых и новейших Redis, RabbitMQ и Kafka, по заданию на версию Python | `make test_brokers_matrix_up`, `make matrix_venvs`, `make test_matrix` |
| `test-poetry-add` | новый проект poetry добавляет hare-orm из checkout, по заданию на ОС (Linux, Windows, macOS) и версию Python | `make deps`, `make test_poetry_add` |
| `ci-passed` | проходит, когда каждое задание выше прошло или пропущено для пул-реквеста из `dev` в `main`, — статус, который требуют правила веток | — |

Задания тестов работают с `rust.native`, собранным из этого коммита, и выкладывают логи `.test-matrix/` как артефакты; вывод упавшего прогона печатается и в логе
задания.

## Стиль кода и проверки

```bash
make style   # автоформатирование (ruff format + ruff check --fix)
make lint    # автоформатирование + проверка типов (mypy) + bandit
make check   # проверка форматирования + ruff + mypy + bandit + rust_check + actionlint + строгая сборка документации без исправлений — то, что выполняет CI
make actionlint                         # процессы GitHub Actions, в закреплённом образе actionlint
make actionlint ACTIONLINT=actionlint   # то же установленным бинарником actionlint
```

actionlint проверяет `.github/workflows/*.yml` — синтаксис, выражения, зависимости заданий и
shell-скрипты шагов через shellcheck (в образе Docker shellcheck есть; с установленным бинарником
shellcheck выполняется, только если он тоже установлен).

Правила проверяются автоматически (ruff, mypy и bandit — в `make check` и CI; codespell — только в хуке
pre-commit) — не спорьте с форматтером,
пусть `make style` всё расставит. Сверх того, что ловят инструменты, в этом коде приняты полные,
описательные имена (без сокращённых переменных и идентификаторов) и подробные, точные докстринги и
комментарии, объясняющие, *почему* код устроен так (неочевидное ограничение, особенность базы
данных), — придерживайтесь этого стиля в новом коде и комментариях.

## Документация

Если изменение затрагивает публичное поведение, обновите страницу в `docs/`, которая за него отвечает, —
и английский файл `.md`, и русский `.ru.md` рядом с ним. Правила ниже действуют для каждой страницы;
`make docs` проверяет большую часть из них (`mkdocs_hooks/docs_standard_checks.py`) и падает на нарушении:

```bash
make docs    # mkdocs build --strict: битая ссылка или якорь либо страница, нарушающая правила ниже
```

### Где что описано

У каждого понятия есть одна страница-владелец — полное описание, аргументы, ошибки и различия баз.
Любая другая страница называет его одним предложением и ссылается туда. Страница-указатель
(`models/meta-options`, `dialects/dialects-and-features`, главная) даёт каждому пункту одно предложение
и ссылку на владельца. Страницы сгруппированы в папки по назначению: `dialects/<database>/` — то, что
относится к одной базе, — `connecting.md` и `differences.md` в каждой такой папке, `fields.md`/`types.md`,
`functions.md` и `testing.md`, где есть что сказать. На каждую страницу ведёт ссылка хотя бы с одной
другой страницы, а не только из навигации. `docs/benchmarks/` и раздел «Бенчмарки» в README пишет
`benchmarks/bench.py` — меняйте генератор, а не сгенерированный текст.

### Как устроена страница

- **Страница-руководство** (большинство страниц): заголовок; вводный абзац из одного–трёх предложений
  о том, что это и когда нужно; минимальный рабочий пример; разделы `##` по задачам.
- **Справочная страница** (`querying/queryset-methods`, `models/field-types`, `querying/functions`,
  `migrations/operations`, `errors/exceptions`, `models/meta-options`, `migrations/cli`): каждый пункт
  устроен одинаково — заголовок, сигнатура, таблица аргументов, что он делает, что поднимает, чем
  отличаются базы. Обзорная таблица в начале даёт каждому пункту одно предложение и ссылку на его раздел.
- **Страница базы** (`dialects/<database>/`): заголовок — `<База>: <тема>` (`ClickHouse: типы`); пункт
  навигации — только тема.
- **Страница интеграции**: что установить (extra, самая старая поддерживаемая версия), как подключить к
  приложению, что она даёт; общее для всех веб-фреймворков описано в `integrations/web-frameworks`, на
  него ссылаются, а не повторяют.

### Заголовки

- У каждого `##` и `###` есть явный идентификатор, общий для обоих языков, — встроенный якорь, который
  понимает и GitHub: `## <a id="some-heading"></a>Some heading`. Сайт делает его идентификатором самого
  заголовка. Глубже `###` заголовков нет.
- Заголовки пишутся как предложение: с заглавной только первое слово. Раздел об одном имени API —
  `` Смысл: `name` `` (`` Транзакция на запрос: `atomic_requests` ``); пункт справочника — одно имя
  (`` `select_for_update()` ``). Без хвоста «— описание».
- Раздел об одной базе заканчивается её именем в скобках: `Смена учётных данных (PostgreSQL)`.

### Различия баз

Если возможность есть не на всех базах, первый абзац её раздела говорит, где она есть и что делают
остальные базы: они поднимают `UnSupportedError` с объяснением почему до отправки SQL — hare не
повторяет то, чего нет в языке базы. База, которая пишет то же самое своей равносильной формой или
выполняет функцию hare своими средствами, различия не создаёт, и описывать его не нужно. Несколько различий сводятся в таблицу `База | Поведение`. Версия называется, только
если она новее самого старого сервера, который hare поддерживает для этой базы
(`*_MINIMUM_SERVER_VERSION`): «SQLite 3.37» — да, «SQLite 3.35» — нет. Таблица всех возможностей по
базам есть только в `dialects/dialects-and-features`.

### Текст

- Описывайте, что код делает сейчас, как правило hare: без истории («теперь», «больше не», «раньше»),
  без сравнения с другими ORM (для этого есть одна страница — `getting-started/coming-from-django`),
  без заметок об обновлении.
- Библиотека в тексте — `hare`; пакет и репозиторий — `hare-orm`; `Hare` — класс.
- Пишите «база данных», а не «БД»; «имя подключения», а не «название подключения»; драйверы называются
  `rust_pg` и `asyncpg`. Тире в тексте — «—», а не « - ».
- Примечание или предупреждение — это GitHub-алерт: `> [!NOTE]` для побочного знания, `> [!WARNING]`
  для потерянных данных или неверного результата, `> [!CAUTION]` для действия, которое нельзя отменить;
  заголовок — жирная первая строка, поэтому страница выглядит одинаково на GitHub и в предпросмотре
  редактора; сайт показывает алерт как admonition с этой строкой в заголовке.
- Переносите текст примерно на 105 символах, без рваных коротких строк посреди абзаца. Таблицы,
  заголовки, код и ссылка, которую нельзя разорвать, остаются на одной строке. Больше ничего не стоит с
  отступом в четыре пробела вне списка или блока кода: обычный Markdown читает такое как код.

### Примеры и блоки кода

- У каждого блока кода указан язык. Блок `python` разбирается как Python; вывод, SQL и прочий текст —
  `text`, `sql`, `bash`.
- Сигнатура пишется заглушкой, чтобы сборка сверила её с кодом: `def name(argument: Type = default) ->
  Result: ...`, `async def ...: ...`, класс — `class Name:` со своим `def __init__(...) -> None: ...`.
  Фрагмент конфигурации — правильный Python: `{"routers": ["app.routers.PrimaryReplicaRouter"],
  **settings}`, никогда `{..., }`.
- У примеров одна предметная область — библиотека: `Author`, `Book`, `Publisher`, `Store`, `Review`.
  Другая — только там, где этого требует тема: арендаторы (`Company`, `Order`), аналитика ClickHouse
  (`PageView`), геоданные (`Place`). Никогда — модель или поле реального проекта.
- Модели импортируют `from hare import fields` и `from hare.models import Model`; любой другой импорт,
  нужный примеру, выписан. Фрагмент выполняется внутри `async`-кода с инициализированным hare
  (`getting-started/first-model` говорит это один раз).
- Имена пишутся полностью — `connection`, `error`, `database`; объект ORM — `obj`, несколько — `objs`.
- В разделе есть текст: блок кода никогда не стоит один, без предложения о том, что он показывает.

### Таблицы и ссылки

- Ячейка таблицы содержит одно-два предложения (около 300 символов); больше — в раздел, на который
  ссылается ячейка. Таблицы аргументов озаглавлены `Аргумент | Что задаёт` (`Параметр`, `Опция`,
  `Настройка` для параметра URL, опции `Meta`, настройки), со столбцом `По умолчанию`, где умолчания
  важны; на английском — `Argument | Meaning` (`Parameter`, `Option`, `Setting`, `Default`).
- Ссылки относительные и ведут на якорь: `[Транзакции](../connections/transactions.ru.md#on-commit)`.
  Русская страница ссылается на страницу `.ru.md` — иначе GitHub откроет английскую, а
  `mkdocs build --strict` этого не заметит.
- Ссылка в ячейке таблицы или пункте списка — «— см. [Тема](...)»; внутри предложения — «(см.
  [Тема](...))»; отдельно — «См. [Тема](...).»

### Русские страницы

Русская страница — это переведённая английская: те же заголовки и якоря в том же порядке, те же
абзацы, списки, таблицы и код. Код не переводится — ни заполнители (`<fields>`), ни код в строке;
комментарии в блоках кода переводятся. Термины:

| English | Russian |
|---|---|
| row | строка |
| column | колонка |
| connection (an alias, its client) | подключение |
| a connection of a pool | соединение |
| relation | связь |
| tenant | арендатор |
| cache | кэш |
| collation | правило сравнения |
| raw SQL | готовый SQL |
| savepoint | точка сохранения (`SAVEPOINT` в SQL) |
| rollback | откат |
| observer | наблюдатель |
| callback | колбэк |
| hook | хук |
| lookup | оператор фильтра (в коде как есть) |
| `QuerySet`, `Meta` | не переводятся |

Тире — «—», кавычки — «ёлочки». Ссылки читаются как «— см. [..]», «(см. [..])», «См. [..].»

## Как прислать изменение

1. Сделайте форк репозитория и ветку от `dev`. Внесите изменение и добавьте или обновите тесты к
   нему. `make test_sqlite` должен оставаться зелёным; если изменение
   затрагивает PostgreSQL — и `make test_postgres_asyncpg`.
2. Для каждого изменения, заметного пользователю, добавьте строку в раздел `## [Unreleased]` файла
   [CHANGELOG.md](CHANGELOG.ru.md) (журнал ведётся на английском) в нужную группу
   (*Breaking changes*, *Added*, *Changed*, *Fixed*, *Removed*, *Security*) и ту же строку
   по-русски в [CHANGELOG.ru.md](CHANGELOG.ru.md). Внутренней переделке и изменениям только в
   тестах запись не нужна.
3. Подпишите каждый коммит (`git commit -s`, см. [Подпись](#подпись)).
4. Выполните `make check` или запушьте и дайте CI сделать это: CI выполняет те же проверки и
   всю [матрицу тестов](#матрица-тестов-и-ci).
5. Откройте пул-реквест в `dev`. Опишите, *зачем* нужно изменение, а не только что оно делает, и
   укажите issue, которое оно закрывает.

Нашли уязвимость? См. [SECURITY](SECURITY.ru.md) — пожалуйста, не открывайте для неё публичный
issue.

## Подпись

hare-orm распространяется по лицензии MIT, и вклад принимается на тех же условиях. Чтобы
подтвердить, что вы вправе прислать его на этих условиях, каждый коммит содержит строку
`Signed-off-by` с вашим именем и почтой — это подтверждение
[Developer Certificate of Origin 1.1](https://developercertificate.org/):

```text
Signed-off-by: Jane Doe <jane@example.com>
```

`git commit -s` добавляет эту строку сам. Процесс `dco` проверяет каждый коммит пул-реквеста и
падает, если строки нет или в ней указан не автор коммита; коммиты Dependabot не проверяются. Чтобы
подписать уже сделанные коммиты:

```bash
git rebase --signoff origin/dev
git push --force-with-lease
```

Код, скопированный из другого проекта, принимается только под лицензией, совместимой с MIT, с
сохранённым уведомлением об авторских правах и упоминанием в [NOTICE](NOTICE.ru.md).

## Выпуски

Выпуск делают мейнтейнеры из ветки `dev`:

1. Выберите версию по [правилам версий](README.ru.md#версии-и-выпуски): патч-выпуск, если есть
   только исправления, минорный — если есть что-то в *Added* или *Breaking changes*.
2. В [CHANGELOG.md](CHANGELOG.ru.md) и [CHANGELOG.ru.md](CHANGELOG.ru.md) переименуйте
   `## [Unreleased]` в `## [X.Y.Z] - YYYY-MM-DD`, добавьте над ним новый пустой `## [Unreleased]` и
   обновите ссылки сравнения внизу.
3. Задайте версию командой `poetry version X.Y.Z` и в `__version__` файла `hare/__init__.py` и
   закоммитьте изменённые файлы как `release: vX.Y.Z`.
4. Перемотайте `main` на этот коммит и запушьте обе ветки.
5. Поставьте тег на вершину `main` и запушьте только этот тег:

   ```bash
   git tag -a vX.Y.Z -m "hare-orm X.Y.Z"
   git push origin vX.Y.Z
   ```

После этого процесс `release` проверяет, что тег совпадает с версией в `pyproject.toml`, что в
журнале изменений есть раздел для неё и что помеченный коммит находится в `main`, и прогоняет на этом
коммите всю [матрицу тестов](#матрица-тестов-и-ci) — без неё ничего не публикуется. На раннере каждой
платформы — Linux x86-64 и ARM64 (manylinux2014 и musllinux 1.2), macOS Intel и Apple Silicon,
Windows x64 и ARM64 — он собирает расширение на Rust для Python 3.12, 3.13 и 3.14 (maturin
`--interpreter` со всеми тремя) и wheel hare-orm с ним на каждую версию Python
(`.github/scripts/build_wheel.py`), а затем ставит каждый wheel в чистое окружение его версии Python,
чтобы выполнить запросы hare к SQLite, а на Linux ещё и к PostgreSQL через драйвер на Rust (wheel
musllinux — в образах `python:3.X-alpine`). Кроме того, он собирает универсальный wheel для остальных платформ и sdist — в них нет
собранных расширений. Публикация на PyPI ждёт одобрения мейнтейнером окружения `pypi`; затем
создаётся выпуск на GitHub с текстом раздела журнала изменений.

Чтобы проверить сборку выпуска без публикации, запустите процесс `release` вручную (Actions →
release → Run workflow): он соберёт и проверит все wheel и на этом остановится.

Исправление уязвимости выпускается патч-выпуском последней минорной версии по правилам
[SECURITY](SECURITY.ru.md).
