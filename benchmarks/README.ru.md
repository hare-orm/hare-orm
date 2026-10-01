# Производительность

[English](README.md)

`bench.py` сравнивает hare-orm с SQLAlchemy, tortoise-orm, yara-orm и Django на одних и тех же
сценариях с одним сервером PostgreSQL. Результаты и графики — на странице
[Производительность](https://hare-orm.github.io/hare-orm/ru/benchmarks/) документации.

## Что измеряется

| Цель | Что запускается |
|---|---|
| `hare-rust` | hare-orm на своём Rust-драйвере PostgreSQL (`postgresql://`) |
| `hare-asyncpg` | hare-orm на asyncpg (`postgresql+asyncpg://`) |
| `sqlalchemy` | асинхронная ORM SQLAlchemy 2 на asyncpg; сессия выполняет команды в транзакции и фиксирует её — поведение SQLAlchemy по умолчанию |
| `sqlalchemy-autocommit` | то же, движок в `AUTOCOMMIT`: каждая команда фиксируется сразу, как в hare и Django; сценарий с транзакциями всё равно идёт в настоящей транзакции |
| `tortoise` | tortoise-orm на asyncpg |
| `yara-orm` | yara-orm |
| `django` | асинхронная ORM Django на psycopg 3 с его пулом подключений |

<!-- benchmark-scenarios:start -->
У каждого сценария одно имя и одна и та же работа во всех ORM; он написан так, как его пишет
документация этой ORM. В таблице 1000 виджетов (`--size small`: 100), у каждого — гаджет (внешний
ключ), метка (многие-ко-многим) и документ JSON. Сценарии — так, как они подписаны на графиках:

- **Чтение**
  - Все 1000 строк
  - 200 × get() по ключу
  - Страница, LIMIT/OFFSET
  - Фильтр по двум полям
  - exists()
  - values_list(flat=True)
  - only("id", "name")
  - OR + JOIN + distinct
  - id IN (200 значений)
  - Чтение JSON-поля
- **Связи**
  - select_related, JOIN
  - prefetch_related
  - N+1: 200 отдельных запросов
  - 200 × add() многие-ко-многим
- **Агрегаты**
  - count()
  - Sum, Avg, Max, Min
  - GROUP BY
  - annotate + values
  - Case / When
- **Запись**
  - bulk_create, 1000 строк
  - 200 × create()
  - update() по фильтру
  - 200 × get() + save()
  - bulk_update, 200 строк
  - Upsert, 200 строк
  - 200 × get_or_create()
  - delete() по фильтру
  - 200 × get() + delete()
  - Запись JSON-поля
- **Транзакции, параллельность, запуск**
  - 200 × транзакция: get() + save()
  - 200 × get() одновременно
  - Запуск: init и первое подключение
- **Нагрузка** — 50 задач выполняют 1000 операций на одном пуле подключений: 80% — `get()`, 15% —
  чтение с фильтром, 5% — `get()` + `save()`.
<!-- benchmark-scenarios:end -->

## Как измеряется

<!-- benchmark-method:start -->
- У каждой ORM пул из 50 подключений; все они открываются до начала замеров.
- Внутри прогона сценарий повторяется 3–5 раз, и в зачёт идёт самый быстрый повтор; удаления и
  `add()` многие-ко-многим меняют данные безвозвратно и выполняются один раз.
- Каждая ORM работает в отдельном процессе. Прогоны идут по кругу, в каждом круге ORM запускаются в
  случайном порядке; результат сценария — медиана по прогонам.
- SQLAlchemy измеряется дважды: с транзакцией вокруг каждой сессии (её поведение по умолчанию) и с
  движком в режиме autocommit, где каждая команда фиксируется сразу, как в hare и Django.
- Каждый сценарий написан так, как его пишет документация этой ORM.
<!-- benchmark-method:end -->

`all` запускает каждую ORM один раз, `--runs N` — N раз. Тогда результат — медиана, а на
графиках виден разброс от лучшего прогона к худшему.

## Как запустить

Нужен сервер PostgreSQL на `127.0.0.1:5433` с пользователем и паролем `postgres` (`make test_db_up`
поднимает такой); каждый прогон создаёт там свою базу данных и удаляет её.

```sh
python -m venv .bench-venv
.bench-venv/bin/pip install -e . -r benchmarks/requirements.txt   # на Windows — .bench-venv\Scripts\pip
.bench-venv/bin/python benchmarks/bench.py all
```

`all` пишет `docs/assets/benchmarks/results.json` — медианы, числа каждого прогона, версии и
машину — и рисует рядом графики. Остальные команды:

```sh
python benchmarks/bench.py run hare-rust       # один прогон одной ORM, вывод на экран
python benchmarks/bench.py all --targets hare-rust django --runs 3
python benchmarks/bench.py charts              # заново нарисовать графики по results.json
```

`--port` указывает на другой сервер. Здесь не измеряются: сетевые задержки, несколько процессов или
ядер, расход памяти.
