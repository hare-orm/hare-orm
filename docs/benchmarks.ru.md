<!-- Written by benchmarks/bench.py (`all` or `charts`): edit the text there, not here. -->

# Производительность

hare-orm против SQLAlchemy, tortoise-orm, yara-orm и Django на одних и тех же сценариях (всего 32):
чтение, связи, агрегаты, запись, транзакции и запуск, а также параллельная нагрузка — с одним
сервером PostgreSQL. Чем короче столбец, тем быстрее; исключение — нагрузка, где считаются операции
в секунду.

## Сводка

Во сколько раз каждая ORM в среднем медленнее, чем hare-orm на драйвере asyncpg (более быстром
драйвере hare-orm в этом прогоне). Среднее — это отношение средних геометрических времени по всем
сценариям, кроме запуска. hare-orm на драйвере на Rust тоже есть в этом сравнении.

![Во сколько раз медленнее hare-orm, в среднем](assets/benchmarks/summary-slower-ru-light.svg#gh-light-mode-only)
![Во сколько раз медленнее hare-orm, в среднем](assets/benchmarks/summary-slower-ru-dark.svg#gh-dark-mode-only)

Нагрузка: 50 задач выполняют 1000 операций на одном пуле подключений: 80% — `get()`, 15% — чтение с
фильтром, 5% — `get()` + `save()`. Операций в секунду:

![Операций в секунду под нагрузкой](assets/benchmarks/summary-load-ru-light.svg#gh-light-mode-only)
![Операций в секунду под нагрузкой](assets/benchmarks/summary-load-ru-dark.svg#gh-dark-mode-only)

## По сценариям

У каждого сценария своя шкала: сравнивайте столбцы внутри одного сценария, а не между сценариями.
Лучшее время выделено жирным. Столбец — медиана 5 прогонов, тонкая линия поверх него — разброс от
лучшего прогона к худшему.

### Чтение

![Чтение](assets/benchmarks/reads-ru-light.svg#gh-light-mode-only)
![Чтение](assets/benchmarks/reads-ru-dark.svg#gh-dark-mode-only)

### Связи

![Связи](assets/benchmarks/relations-ru-light.svg#gh-light-mode-only)
![Связи](assets/benchmarks/relations-ru-dark.svg#gh-dark-mode-only)

### Агрегаты

![Агрегаты](assets/benchmarks/aggregates-ru-light.svg#gh-light-mode-only)
![Агрегаты](assets/benchmarks/aggregates-ru-dark.svg#gh-dark-mode-only)

### Запись

![Запись](assets/benchmarks/writes-ru-light.svg#gh-light-mode-only)
![Запись](assets/benchmarks/writes-ru-dark.svg#gh-dark-mode-only)

### Транзакции, параллельность, запуск

![Транзакции, параллельность, запуск](assets/benchmarks/transactions-ru-light.svg#gh-light-mode-only)
![Транзакции, параллельность, запуск](assets/benchmarks/transactions-ru-dark.svg#gh-dark-mode-only)

## Где и как измерялось

| | |
|---|---|
| Дата | 2026-10-03 |
| Машина | Windows 11, 13th Gen Intel(R) Core(TM) i5-13400, 16 CPU |
| Python | 3.14.3 |
| PostgreSQL | 18.6 (Debian 18.6-1.pgdg13+2) |
| asyncpg | 0.31.0 |
| hare-orm | 0.9.0 |
| SQLAlchemy | 2.1.1 |
| tortoise-orm | 1.1.8 |
| yara-orm | 1.17.0 |
| Django | 6.1.1 |
| Коммит hare-orm | `70ed3cd` |
| Прогонов | 5, медиана |
| Строк в таблице | 1000 |

- У каждой ORM пул из 50 подключений; все они открываются до начала замеров.
- Внутри прогона сценарий повторяется 3–5 раз, и в зачёт идёт самый быстрый повтор; удаления и
  `add()` многие-ко-многим меняют данные безвозвратно и выполняются один раз.
- Каждая ORM работает в отдельном процессе. Прогоны идут по кругу, в каждом круге ORM запускаются в
  случайном порядке; результат сценария — медиана по прогонам.
- SQLAlchemy измеряется дважды: с транзакцией вокруг каждой сессии (её поведение по умолчанию) и с
  движком в режиме autocommit, где каждая команда фиксируется сразу, как в hare и Django.
- Каждый сценарий написан так, как его пишет документация этой ORM.

Стенд — [`benchmarks/bench.py`](https://github.com/hare-orm/hare-orm/blob/dev/benchmarks/bench.py) в
репозитории; в его [README](https://github.com/hare-orm/hare-orm/blob/dev/benchmarks/README.ru.md)
перечислены все сценарии и описано, как повторить замер на своей машине.
