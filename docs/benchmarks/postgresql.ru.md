<!-- Written by benchmarks/bench.py (`all` or `charts`): edit the text there, not here. -->

# PostgreSQL

hare-orm против SQLAlchemy, tortoise-orm, yara-orm и Django на PostgreSQL: 50 сценариев, таблица из
1 000 виджетов, у каждого — гаджет, метка и документ JSON, и параллельная нагрузка. Чем короче
столбец, тем быстрее; исключение — нагрузка, где считаются операции в секунду.

## <a id="summary"></a>Сводка

Во сколько раз каждая библиотека в среднем медленнее, чем hare-orm на драйвере на Rust (более
быстром драйвере hare-orm в этом прогоне): отношение средних геометрических времени по всем
сценариям, которые выполняют обе, кроме запуска.

![Во сколько раз медленнее hare-orm, в среднем](../assets/benchmarks/postgresql-summary-slower-ru-light.svg#gh-light-mode-only)
![Во сколько раз медленнее hare-orm, в среднем](../assets/benchmarks/postgresql-summary-slower-ru-dark.svg#gh-dark-mode-only)

Нагрузка: 50 задач выполняют 1 000 операций на одном пуле подключений: 80% — `get()`, 15% — чтение с
фильтром, 5% — `get()` + `save()`; в нагрузке с записью половина операций — `get()` + `save()`.
Замер «на горячую»: до начала замера каждое подключение пула выполняет каждую операцию нагрузки, как
в уже работающем приложении.

Операций в секунду:

![Операций в секунду](../assets/benchmarks/postgresql-summary-load-test-ru-light.svg#gh-light-mode-only)
![Операций в секунду](../assets/benchmarks/postgresql-summary-load-test-ru-dark.svg#gh-dark-mode-only)

Операций в секунду, нагрузка с записью:

![Операций в секунду, нагрузка с записью](../assets/benchmarks/postgresql-summary-write-load-test-ru-light.svg#gh-light-mode-only)
![Операций в секунду, нагрузка с записью](../assets/benchmarks/postgresql-summary-write-load-test-ru-dark.svg#gh-dark-mode-only)

## <a id="by-scenario"></a>По сценариям

У каждого сценария своя шкала: сравнивайте столбцы внутри одного сценария, а не между сценариями.
Лучшее время выделено жирным. Столбец — медиана 3 прогонов, тонкая линия поверх него — разброс от
лучшего прогона к худшему.

### <a id="reads"></a>Чтение

![Чтение](../assets/benchmarks/postgresql-reads-ru-light.svg#gh-light-mode-only)
![Чтение](../assets/benchmarks/postgresql-reads-ru-dark.svg#gh-dark-mode-only)

### <a id="relations"></a>Связи

![Связи](../assets/benchmarks/postgresql-relations-ru-light.svg#gh-light-mode-only)
![Связи](../assets/benchmarks/postgresql-relations-ru-dark.svg#gh-dark-mode-only)

### <a id="aggregates"></a>Агрегаты

![Агрегаты](../assets/benchmarks/postgresql-aggregates-ru-light.svg#gh-light-mode-only)
![Агрегаты](../assets/benchmarks/postgresql-aggregates-ru-dark.svg#gh-dark-mode-only)

### <a id="writes"></a>Запись

![Запись](../assets/benchmarks/postgresql-writes-ru-light.svg#gh-light-mode-only)
![Запись](../assets/benchmarks/postgresql-writes-ru-dark.svg#gh-dark-mode-only)

### <a id="transactions"></a>Транзакции, параллельность, запуск

![Транзакции, параллельность, запуск](../assets/benchmarks/postgresql-transactions-ru-light.svg#gh-light-mode-only)
![Транзакции, параллельность, запуск](../assets/benchmarks/postgresql-transactions-ru-dark.svg#gh-dark-mode-only)

## <a id="where-and-how-it-was-measured"></a>Где и как измерялось

| | |
|---|---|
| Дата | 2026-10-09 |
| Машина | Windows 11, 13th Gen Intel(R) Core(TM) i5-13400, 16 CPU |
| Python | 3.14.3 |
| Коммит hare-orm | `a4013db4` |
| Прогонов | 3, медиана |
| PostgreSQL | 18.6 (Debian 18.6-1.pgdg13+2) |
| asyncpg | 0.31.0 |
| psycopg | 3.3.6 |
| hare-orm | 0.9.0 |
| SQLAlchemy | 2.1.1 |
| tortoise-orm | 1.1.8 |
| yara-orm | 1.17.0 |
| Django | 6.1.1 |
| Строк в таблице | 1 000 |

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

Стенд — [`benchmarks/bench.py`](https://github.com/hare-orm/hare-orm/blob/dev/benchmarks/bench.py) в
репозитории; в его [README](https://github.com/hare-orm/hare-orm/blob/dev/benchmarks/README.ru.md)
перечислены все сценарии и описано, как повторить замер на своей машине.
