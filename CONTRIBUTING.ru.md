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

Хук выполняет `make _check` — те же проверки ruff, mypy, bandit, Rust и actionlint тех же
версий, что и `make check` и задание `check` в CI. Проверкам Rust нужен инструментарий Rust, а
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
| `hare/query/` | Построение запросов: `QuerySet` и классы проверки его аргументов (`queryset/`), команды, которые выполняет queryset (`statements/`: построение — соединения, группировка, сортировка, keyset, блокировки строк, аннотации, условия, CTE; `select/` — values, строки моделей, операции над множествами; `summary/`; `write/` — обновление, удаление, массовые записи, merge), планы команд и то, как части запроса их описывают (`plans/`), переписывания, которые каждая точка входа применяет один раз (`rewrites/`), выражения (`expressions/`), функции ORM (`functions/`), операторы фильтров и фильтры (`filters/`), менеджеры, области видимости, загрузка связей, пути и API описания фильтров (`lookup_info/`). |
| `hare/sql/` | Построитель SQL, через который выводит ORM: термины, условия, функции и построители запросов (`builder/`) — без моделей и подключений. |
| `hare/ddl/` | Объекты схемы, объявляемые в `Meta`: индексы, ограничения, триггеры, параметры таблиц и нейтральное к диалекту экранирование имён и литералов. |
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
`forget_all()`. `tests/test_model_caches.py` падает, если вместо них объявлен `WeakKeyDictionary` с
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
make test            # весь набор на SQLite, с покрытием
make test_sqlite     # то же без отчёта о покрытии (быстрее при локальной работе)
make test_columnar   # весь набор на тестовом колоночном диалекте
```

Тестовый колоночный диалект (`tests/dialects/columnar`) — сторонний диалект, собранный только из
публичного API hare: без транзакций, внешних ключей и уникальных ограничений, со своими
плейсхолдерами, экранированием, типами и схемой адреса (`columnar://`). Изменение, которое работает
на SQLite, но опирается на то, чего не обещает контракт диалекта, здесь падает.

Часть тестов вообще не читает тестовую базу: прогоны плагина mypy, стабы, проверки исходного кода
hare, программы, запускаемые в отдельном процессе. Они помечены `database_independent` и образуют
отдельный набор, который не входит в набор ни одной базы, — поэтому выполняются один раз, а не
заново для SQLite, колоночного диалекта и каждого драйвера PostgreSQL:

```bash
make test_database_independent
```

Новый модуль такого рода помечается `pytestmark = pytest.mark.database_independent`. `make test` и
обычный запуск `pytest` выполняют все тесты, включая эти.

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
make deps options="--extras clickhouse --extras clickhouse-driver"   # оба драйвера ClickHouse, для следующей строки
make test_clickhouse_up && make test_clickhouse            # тесты диалекта ClickHouse на обоих драйверах, ClickHouse 25.8 на портах 8124 (HTTP) и 9124 (родной протокол)
make test_brokers_up && make test_brokers                  # outbox на Redis (6390), RabbitMQ (5673), Kafka (9095)
```

PgBouncer работает в режиме пула транзакций с подготовленными командами между транзакциями (1.21+,
самая старая версия, которую поддерживает hare); то, чему нужен свой сеанс (`LISTEN`, тайм-аут
блокировки сеанса), идёт напрямую на сервер. Для ClickHouse выполняются только собственные тесты
диалекта (`tests/dialects/clickhouse`): у общих тестовых моделей ключи генерирует база, а ClickHouse
этого не умеет. Без своих серверов тесты брокеров пропускаются.

Каждая цель `make` и каждый прогон матрицы передают `--verify-plans` (`tests/plan_verification`):
каждый запрос, выполненный по закэшированному плану — плану команды или плану по сигнатуре вызовов, —
строится заново без планов, и его SQL и параметры сравниваются с планом. Расхождение роняет тест с
обеими командами и ключом плана, так что план, забывший часть запроса, ловит тот тест, который его
выполнил. Передавайте опцию и своему запуску `pytest`:

```bash
HARE_TEST_DB=sqlite+aiosqlite://:memory: poetry run pytest --verify-plans tests/test_queryset.py
```

`make testall`/`make ci` выполняет `check`, а также `test_database_independent`, `test_sqlite`, `test_columnar` и
`test_postgres_asyncpg` — всё, кроме `test_postgres_rust`, которому нужен инструментарий Rust. CI
выполняет набор на версиях Python, SQLite и PostgreSQL [матрицы тестов](#матрица-тестов-и-ci),
через оба драйвера PostgreSQL: на всех — для пул-реквеста в `main`, каждую ночь, по запросу и перед
выпуском; в остальных случаях — на новейших.

## Сборка расширений на Rust

Для `test_postgres_rust` и любой локальной работы с `rust/` нужны [rustup](https://rustup.rs/) и
[maturin](https://www.maturin.rs/). `rust-toolchain.toml` закрепляет версию Rust, которую использует и
CI, — rustup ставит её при первом вызове `cargo` в checkout, поэтому проверка, прошедшая у вас, проходит и
в CI:

```bash
make build_native           # rust.native для текущей ОС и текущего Python, в rust/
make build_native_linux     # rust.native для Linux и Python 3.12-3.14, в Docker
make build_native_windows   # rust.native для Windows и Python 3.12-3.14 (.venv-3.X)
make build_native_macos     # rust.native для macOS и Python 3.12-3.14 (.venv-3.X), universal2
make rust_check             # cargo fmt --check, clippy и модульные тесты Rust (входит в make check)
```

`rust.native` (`rust/native/`) содержит драйвер PostgreSQL для адресов `postgresql://` (`rust.native.pg`)
и чтение и запись строк с кодеками полей (`rust.native.rows`). Во время работы он необязателен: без него
используйте адреса `postgresql+asyncpg://`, а строки читаются и пишутся на чистом Python. CI собирает его
при каждом запуске для Python 3.12, 3.13 и 3.14 на Linux, Windows и macOS (под macOS — один файл
universal2 на версию Python для Apple silicon и Intel), передаёт всем заданиям, которым он нужен, и
коммитит девять файлов (`rust/native.cpython-3XX-x86_64-linux-gnu.so`, `rust/native.cp3XX-win_amd64.pyd`
и `rust/native.cpython-3XX-darwin.so`) обратно в `dev` и `main` — checkout на любой из трёх систем
работает без инструментария Rust, а пересобирать нужно, только если вы меняли исходники в `rust/`.

## Матрица тестов и CI

Матрица тестов — все сочетания, на которых выполняется набор тестов:

| Ось | Версии |
| --- | --- |
| Python | 3.12, 3.13, 3.14 |
| SQLite | 3.35.5 (самая старая поддерживаемая), 3.37.2 (собственная в Ubuntu 22.04; таблицы `STRICT` с 3.37), 3.38.0 (`->>`), 3.41.0 (`unhex()`) и 3.53.4 (новейшая), а также новейшая с установленными функциями регулярных выражений SQLite; на Linux, Windows и macOS |
| PostgreSQL | 14, 15, 16, 17, 18 — каждая через `asyncpg` (`postgresql+asyncpg://`) и через драйвер на Rust (`postgresql://`); на Linux |
| Тестовый колоночный диалект | на Linux |

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

### Матрица на своей машине

Нужны Python 3.12, 3.13 и 3.14 (`py -3.X` на Windows, `python3.X` на остальных системах), Docker для
серверов PostgreSQL, а на Linux и macOS — компилятор C для библиотек SQLite. Прогоны rust_pg берут
сборки `rust.native` из `rust/`: файлы для Linux, Windows и macOS из репозитория или
`make build_native_linux`/`make build_native_windows`/`make build_native_macos` после изменений в
`rust/`.

```bash
make matrix_venvs         # .venv-3.12, .venv-3.13, .venv-3.14 со всеми группами зависимостей
make sqlite_versions      # все версии SQLite, подготовленные для каждого из этих Python
make test_db_matrix_up    # PostgreSQL 14-18: hare-orm-test-db-14 ... -18 на портах 5414-5418
make test_matrix          # все прогоны, затем таблица: Python x набор -> прошёл/упал, счётчики, время
make test_db_matrix_down  # удаляет серверы
```

Каждый прогон пишет вывод pytest в `.test-matrix/<python>-<набор>.log`; `make test_matrix` падает,
если упал хотя бы один прогон. Наборы — `database-independent`, `sqlite-<версия>`,
`sqlite-regexp`, `columnar`, `asyncpg-<postgres>` и `rust_pg-<postgres>`. Часть матрицы выполняется с параметрами
`tests/matrix/matrix_runner.py`, а подготовительные цели сужаются своими переменными:

```bash
make test_matrix MATRIX_OPTIONS="--python 3.13 --suite 'sqlite-*' --suite rust_pg-16"
make test_matrix MATRIX_OPTIONS="--workers auto --print-failed-logs"   # по умолчанию 8 процессов
make test_matrix MATRIX_OPTIONS="--newest"   # только новейшие Python, SQLite и PostgreSQL
make sqlite_versions matrix_sqlite_versions=newest
make matrix_venvs matrix_python_versions=3.13 matrix_interpreter=/usr/bin/python3.13
make sqlite_versions matrix_python_versions=3.13 matrix_sqlite_versions=3.35.5
make test_db_matrix_up matrix_postgres_versions=16
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
проверяются и на самых низких версиях, которые допускают их объявленные границы, на Python 3.12.
Нижняя граница — это обещание: если hare нужно больше, граница в `pyproject.toml` поднимается.

```bash
make test_db_up                 # тестовый сервер на 5433
make lowest_dependencies_venv   # .venv-lowest (LOWEST_DEPENDENCIES_VENV=<путь> — в другом месте)
make test_lowest_dependencies   # набор на SQLite и на PostgreSQL через оба драйвера
```

`lowest_dependencies_venv` нужен [uv](https://docs.astral.sh/uv/): зависимости разрешаются через
`uv pip compile --resolution lowest-direct` в `.venv-lowest/requirements.txt` и ставятся оттуда.
Зависимость, которую другая требует в более новой версии, получает эту более новую версию (Robyn
требует `orjson` 3.11.5 и `uvloop` 0.22, FastAPI — `pydantic` 2.7), — в файле видно, что
поставлено на самом деле. Инструменты тестов (`pytest` с плагинами, `pytz`, `ruff` — он форматирует файлы миграций, которые читают тесты) не зависимости hare и
остаются на версиях из `poetry.lock` (`tests/lowest_dependencies/locked_tool_requirements.py`).

### CI

CI (`.github/workflows/ci.yml`) запускается для каждого пул-реквеста, каждого пуша в `dev` и `main`,
каждую ночь (на `dev`), по запросу (*Run workflow*) и перед выпуском (его вызывает процесс
`release`). Какую часть матрицы проверяет запуск, решает его задание `plan`:

- **всю матрицу** — пул-реквест в `main`, ночной запуск, запуск по запросу, выпуск;
- **её новейший угол** — пуш, пул-реквест в другую ветку: Python 3.14, новейшая SQLite и
  `sqlite-regexp` на Linux, колоночный диалект, PostgreSQL 18 через оба драйвера
  (`make test_matrix MATRIX_OPTIONS=--newest`).

Каждое задание выполняет цели Makefile, описанные выше, — задания тестов вместе и есть
`make test_matrix`, каждое — его часть:

| Задание | Что делает | Цели Makefile |
| --- | --- | --- |
| `plan` | выбирает матрицу запуска | — |
| `check` | проверка форматирования ruff, ruff, mypy и bandit на Python 3.12; cargo fmt, clippy и модульные тесты Rust; actionlint | `make deps`, `make check` |
| `build-native-linux` | `rust.native` для Python 3.12, 3.13 и 3.14 под Linux, в образе maturin (manylinux2014) | `make build_native_linux` |
| `build-native` | `rust.native` для Python 3.12, 3.13 и 3.14 под Windows и под macOS (universal2), по заданию на систему | `make deps`, `make build_native_windows`, `make build_native_macos` |
| `test-sqlite` | тесты, не читающие тестовую базу (`database-independent`), все версии SQLite и `sqlite-regexp`, по заданию на ОС (Linux, Windows, macOS) и версию Python; в новейшем углу — новейшая SQLite и `sqlite-regexp` на Linux и Python 3.14 | `make matrix_venvs`, `make sqlite_versions`, `make test_matrix` |
| `test-columnar` | тестовый колоночный диалект, по заданию на версию Python (в новейшем углу — Python 3.14) | `make matrix_venvs`, `make test_matrix` |
| `test-postgres` | одна версия PostgreSQL через оба драйвера, по заданию на версию PostgreSQL и Python (в новейшем углу — PostgreSQL 18 на Python 3.14) | `make test_db_matrix_up`, `make matrix_venvs`, `make test_matrix` |
| `test-lowest-dependencies` | нижние версии зависимостей на Python 3.12 — при полной матрице | `make test_db_up`, `make lowest_dependencies_venv`, `make test_lowest_dependencies` |
| `test-pgbouncer` | набор PostgreSQL через PgBouncer, оба драйвера, на Python 3.14 | `make test_db_up`, `make test_pgbouncer_up`, `make test_pgbouncer` |
| `test-clickhouse` | тесты диалекта ClickHouse на ClickHouse 25.8, на Python 3.14 | `make deps`, `make test_clickhouse_up`, `make test_clickhouse` |
| `test-brokers` | обработчики пробуждения и доставки outbox на Redis, RabbitMQ и Kafka, на Python 3.14 | `make test_brokers_up`, `make test_brokers` |
| `test-poetry-add` | новый проект poetry добавляет hare-orm из checkout | `make deps`, `make test_poetry_add` |
| `commit-native-binary` | при пуше в `dev`/`main`, когда все задания запуска прошли: коммитит девять файлов `rust.native` для Linux, Windows и macOS | — |
| `deploy-docs` | при пуше в `dev`: публикует документацию | `make docs` |

Задания тестов работают с `rust.native`, собранным из этого коммита, а не с файлами из репозитория,
и выкладывают логи `.test-matrix/` как артефакты; вывод упавшего прогона печатается и в логе
задания.

## Стиль кода и проверки

```bash
make style   # автоформатирование (ruff format + ruff check --fix)
make lint    # автоформатирование + проверка типов (mypy) + bandit
make check   # проверка форматирования + ruff + mypy + bandit + rust_check + actionlint без исправлений — то, что выполняет CI
make actionlint                         # процессы GitHub Actions, в закреплённом образе actionlint
make actionlint ACTIONLINT=actionlint   # то же установленным бинарником actionlint
```

actionlint проверяет `.github/workflows/*.yml` — синтаксис, выражения, зависимости заданий и
shell-скрипты шагов через shellcheck (в образе Docker shellcheck есть; с установленным бинарником
shellcheck выполняется, только если он тоже установлен).

Правила проверяются автоматически (ruff, mypy, bandit, codespell) — не спорьте с форматтером,
пусть `make style` всё расставит. Сверх того, что ловят инструменты, в этом коде приняты полные,
описательные имена (без сокращённых переменных и идентификаторов) и подробные, точные докстринги и
комментарии, объясняющие, *почему* код устроен так (неочевидное ограничение, особенность базы
данных), — придерживайтесь этого стиля в новом коде и комментариях.

## Документация

Если изменение затрагивает публичное поведение, обновите нужную страницу в `docs/` — и английский
файл `.md`, и русский `.ru.md` рядом с ним. У каждого заголовка есть явный идентификатор, общий
для обоих языков, — встроенный якорь, который понимает и GitHub (`## <a id="some-heading"></a>Some heading`);
сайт делает его идентификатором самого заголовка. Примечание
или предупреждение — это GitHub-алерт (`> [!NOTE]`, `> [!WARNING]`, `> [!CAUTION]`, заголовок — жирная
первая строка), поэтому страница выглядит одинаково на GitHub и в предпросмотре редактора; сайт
показывает его как admonition с этой строкой в заголовке (`mkdocs_hooks/`). Больше ничего не стоит с
отступом в четыре пробела вне списка или блока кода: обычный Markdown читает такое как код. Перед
открытием пул-реквеста проверьте, что сайт собирается:

```bash
make docs    # mkdocs build --strict: падает на битой ссылке или якоре
```

## Как прислать изменение

1. Сделайте форк репозитория и ветку от `dev`. Внесите изменение и добавьте или обновите тесты к
   нему. `make test_sqlite` и `make test_columnar` должны оставаться зелёными; если изменение
   затрагивает PostgreSQL — и `make test_postgres_asyncpg`.
2. Для каждого изменения, заметного пользователю, добавьте строку в раздел `## [Unreleased]` файла
   [CHANGELOG.md](CHANGELOG.ru.md) (журнал ведётся на английском) в нужную группу
   (*Breaking changes*, *Added*, *Changed*, *Fixed*, *Removed*, *Security*) и ту же строку
   по-русски в [CHANGELOG.ru.md](CHANGELOG.ru.md). Внутренней переделке и изменениям только в
   тестах запись не нужна.
3. Подпишите каждый коммит (`git commit -s`, см. [Подпись](#подпись)).
4. Выполните `make check` или запушьте и дайте CI сделать это: CI выполняет те же проверки и
   [матрицу тестов](#матрица-тестов-и-ci) — для пул-реквеста в `dev` её новейший угол.
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
падает, если строки нет или в ней указан не автор коммита. Чтобы подписать уже сделанные коммиты:

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
