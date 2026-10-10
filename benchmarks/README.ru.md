# Производительность

[English](README.md)

`bench.py` сравнивает hare-orm с SQLAlchemy, tortoise-orm, yara-orm, Django и драйвером
clickhouse-connect на одних и тех же сценариях на PostgreSQL, SQLite и ClickHouse. Результаты и графики —
на страницах [Производительность](https://hare-orm.github.io/hare-orm/ru/benchmarks/) документации.

## Что измеряется

| База | Цель | Что запускается |
|---|---|---|
| PostgreSQL | `hare-rust` | hare-orm на своём Rust-драйвере PostgreSQL (`postgresql://`) |
| | `hare-asyncpg` | hare-orm на asyncpg (`postgresql+asyncpg://`) |
| | `sqlalchemy` | асинхронная ORM SQLAlchemy 2 на asyncpg; сессия выполняет команды в транзакции и фиксирует её — поведение SQLAlchemy по умолчанию |
| | `sqlalchemy-autocommit` | то же, движок в `AUTOCOMMIT`: каждая команда фиксируется сразу, как в hare и Django; сценарии с транзакциями всё равно идут в настоящей транзакции |
| | `tortoise` | tortoise-orm на asyncpg |
| | `yara-orm` | yara-orm |
| | `django` | асинхронная ORM Django на psycopg 3 с его пулом подключений |
| SQLite | `hare-sqlite` | hare-orm на aiosqlite |
| | `sqlalchemy-sqlite`, `sqlalchemy-autocommit-sqlite` | асинхронная ORM SQLAlchemy 2 на aiosqlite, в обоих режимах |
| | `tortoise-sqlite` | tortoise-orm |
| | `yara-orm-sqlite` | yara-orm |
| | `django-sqlite` | асинхронная ORM Django |
| ClickHouse | `hare-clickhouse` | hare-orm на clickhouse-connect — HTTP-интерфейс ClickHouse |
| | `hare-clickhouse-driver` | hare-orm на clickhouse-driver — родной протокол ClickHouse поверх TCP |
| | `clickhouse-connect` | драйвер clickhouse-connect сам по себе, текст SQL — нижняя граница, на которой строится ORM поверх него |
| | `sqlalchemy-clickhouse` | асинхронная ORM SQLAlchemy 2.0 с clickhouse-sqlalchemy на драйвере asynch, в своём окружении |
| | `django-clickhouse` | асинхронная ORM Django с django-clickhouse-backend |

<!-- benchmark-scenarios:start -->
У каждого сценария одно имя и одна и та же работа во всех ORM; он написан так, как его пишет
документация этой ORM (`--size small`: таблица из 100 виджетов). Сценарии — так, как они подписаны
на графиках:

- **PostgreSQL** — таблица из 1 000 виджетов, у каждого — гаджет, метка и документ JSON
  - *Чтение*: Все 1000 строк; 200 × get() по ключу; 200 × first(); Страница, LIMIT/OFFSET; order_by() + первые 10; Фильтр по двум полям; Поиск icontains; exists(); values_list(flat=True); distinct() значений поля; only("id", "name"); OR + JOIN + distinct; id IN (200 значений); Обход всех строк порциями; Чтение JSON-поля
  - *Связи*: select_related, JOIN; Фильтр через связь; prefetch_related; prefetch_related многие-ко-многим; Фильтр через многие-ко-многим; annotate(Count) по связи; Exists() в annotate; N+1: 200 отдельных запросов; 200 × add() многие-ко-многим
  - *Агрегаты*: count(); Sum, Avg, Max, Min; Count с условием; GROUP BY; HAVING по агрегату; annotate + values; Case / When; Окно: Rank() по категории
  - *Запись*: bulk_create, 1000 строк; bulk_create, 10000 строк; 200 × create(); update() по фильтру; update() с F(): value + 1; 200 × update() по ключу; 200 × get() + save(); bulk_update, 200 строк; Upsert, 200 строк; 200 × get_or_create(); delete() по фильтру; 200 × get() + delete(); Запись JSON-поля
  - *Транзакции, параллельность, запуск*: 200 × транзакция: get() + save(); 200 × вложенная транзакция (savepoint); 200 × select_for_update() в транзакции; 200 × get() одновременно; Запуск: init и первое подключение
  - *Нагрузка*: 50 задач выполняют 1 000 операций на одном пуле подключений: 80% — `get()`, 15% —
    чтение с фильтром, 5% — `get()` + `save()`; в нагрузке с записью половина операций — `get()` +
    `save()`.
- **SQLite** — таблица из 1 000 виджетов, у каждого — гаджет, метка и документ JSON
  - *Чтение*: Все 1000 строк; 200 × get() по ключу; 200 × first(); Страница, LIMIT/OFFSET; order_by() + первые 10; Фильтр по двум полям; Поиск icontains; exists(); values_list(flat=True); distinct() значений поля; only("id", "name"); OR + JOIN + distinct; id IN (200 значений); Обход всех строк порциями; Чтение JSON-поля
  - *Связи*: select_related, JOIN; Фильтр через связь; prefetch_related; prefetch_related многие-ко-многим; Фильтр через многие-ко-многим; annotate(Count) по связи; Exists() в annotate; N+1: 200 отдельных запросов; 200 × add() многие-ко-многим
  - *Агрегаты*: count(); Sum, Avg, Max, Min; Count с условием; GROUP BY; HAVING по агрегату; annotate + values; Case / When; Окно: Rank() по категории
  - *Запись*: bulk_create, 1000 строк; bulk_create, 10000 строк; 200 × create(); update() по фильтру; update() с F(): value + 1; 200 × update() по ключу; 200 × get() + save(); bulk_update, 200 строк; Upsert, 200 строк; 200 × get_or_create(); delete() по фильтру; 200 × get() + delete(); Запись JSON-поля
  - *Транзакции, параллельность, запуск*: 200 × транзакция: get() + save(); 200 × вложенная транзакция (savepoint); 200 × get() одновременно; Запуск: init и первое подключение
  - *Нагрузка*: 50 задач выполняют 1 000 операций на одном пуле подключений: 80% — `get()`, 15% —
    чтение с фильтром, 5% — `get()` + `save()`; в нагрузке с записью половина операций — `get()` +
    `save()`.
- **ClickHouse** — таблица из 1 000 000 событий, отсортированная по ключу
  - *Чтение*: count() с фильтром; GROUP BY + Sum, Avg; Топ-10 по сумме; Группировка по дню; Число уникальных значений; Страница values(); 200 × get() по ключу; 200 × get() одновременно
  - *Запись и запуск*: Вставка 100000 строк; update() по фильтру (мутация); delete() по фильтру (мутация); Запуск: init и первое подключение
  - *Нагрузка*: 50 задач выполняют 1 000 операций: 80% — `get()` по ключу, 20% — `GROUP BY` за день.
<!-- benchmark-scenarios:end -->

## Как измеряется

<!-- benchmark-method:start -->
- На PostgreSQL у каждой ORM пул из 50 подключений; все они открываются до начала замеров.
- Перед нагрузочным тестом каждое подключение пула вне замера выполняет каждую операцию нагрузки:
  замер показывает работающее приложение, а не только что запущенное, — подключение уже открыто и
  подготовило свои запросы.
- Внутри прогона сценарий повторяется 3–5 раз, и в зачёт идёт самый быстрый повтор; строки, которые
  пишет сценарий записи, удаляются перед каждым повтором вне замера. Удаления и `add()`
  многие-ко-многим меняют данные безвозвратно и выполняются один раз.
- Каждая ORM работает в отдельном процессе. Прогоны идут по кругу, в каждом круге ORM запускаются в
  случайном порядке; результат сценария — медиана по прогонам.
- SQLAlchemy на PostgreSQL и SQLite измеряется дважды: с транзакцией вокруг каждой сессии (её
  поведение по умолчанию) и с движком в режиме autocommit, где каждая команда фиксируется сразу, как
  в hare и Django.
- Сценарий, которого в ORM нет, помечен на графике «нет в этой ORM» и не входит в её среднее.
- Запуск — это инициализация библиотеки и её первый запрос; библиотека к этому моменту уже
  импортирована. В среднее запуск не входит.
- На SQLite каждая ORM работает с одним файлом так, как работает по умолчанию; подключения
  SQLAlchemy ждут блокировку записи файла до 30 с вместо 5 с по умолчанию — иначе нагрузка с записью
  у её пула из пяти подключений обрывается ошибкой «database is locked». `select_for_update()` на
  SQLite не измеряется: блокировок строк в нём нет.
- Таблицу ClickHouse заполняет сам сервер перед сценариями; мутация (`update()`, `delete()`) в
  каждой библиотеке завершается до возврата из вызова. SQLAlchemy работает через
  clickhouse-sqlalchemy 0.3.2 с драйвером asynch 0.2.5 на SQLAlchemy 2.0.30 и Python 3.12 — на более
  новых версиях её асинхронный драйвер не запускается.
- Каждый сценарий написан так, как его пишет документация этой ORM.
<!-- benchmark-method:end -->

`all` запускает каждую цель один раз, `--runs N` — N раз. Тогда результат — медиана, а на графиках
виден разброс от лучшего прогона к худшему.

## Как запустить

Серверы на 127.0.0.1: PostgreSQL на порту 5433 с пользователем и паролем `postgres` (`make test_db_up`
поднимает такой) и ClickHouse с паролем `clickhouse`, HTTP на 8124 и родным протоколом на 9124
(`make test_clickhouse_up`). Каждый прогон создаёт там свою базу данных — или файл SQLite во временном
каталоге — и удаляет её.

```sh
python -m venv .bench-venv
.bench-venv/bin/pip install -e . -r benchmarks/requirements.txt   # на Windows — .bench-venv\Scripts\pip
.bench-venv/bin/python benchmarks/bench.py all
```

Цель `sqlalchemy-clickhouse` работает в своём окружении, `.bench-venv-clickhouse-sqlalchemy`:
асинхронный драйвер clickhouse-sqlalchemy запускается только на SQLAlchemy 2.0.30 и Python 3.12, а
остальные цели SQLAlchemy измеряют её текущую версию.

```sh
python3.12 -m venv .bench-venv-clickhouse-sqlalchemy               # на Windows — py -3.12
.bench-venv-clickhouse-sqlalchemy/bin/pip install -r benchmarks/requirements-clickhouse-sqlalchemy.txt
```

`all` пишет `docs/assets/benchmarks/results.json` — медианы, числа каждого прогона, версии и машину —
рисует рядом графики и пишет страницы в `docs/benchmarks/`. Остальные команды:

```sh
python benchmarks/bench.py run hare-rust --size small    # один прогон одной цели, вывод на экран
python benchmarks/bench.py all --databases sqlite clickhouse --runs 3
python benchmarks/bench.py all --targets hare-rust django
python benchmarks/bench.py charts                        # заново графики и страницы по results.json
```

Здесь не измеряются: сетевые задержки, несколько процессов или ядер, расход памяти.
