# OpenTelemetry

Открывает настоящий интервал трассировки (span) OpenTelemetry вокруг каждого запроса, который
выполняют клиенты драйверов hare-orm, правильно вложенный в тот интервал, который уже активен, — так
же, как вручную написанный `with tracer.start_as_current_span(...)` вокруг запроса.
А ещё он сообщает метрики каждого пула соединений ([Метрики пула и здоровье](pool-health.ru.md#opentelemetry-metrics)).

```bash
pip install hare-orm[opentelemetry]
```

```python
from hare.contrib.opentelemetry import OpenTelemetryInstrumentor

instrumentor = OpenTelemetryInstrumentor()
instrumentor.instrument()
...
instrumentor.uninstrument()
```

## <a id="why-not-query-hooks"></a>Обёртка запроса, а не наблюдатель

hare-orm сообщает о каждом завершённом запросе своим [наблюдателям](observers.ru.md) событием
`QueryExecuted`. Интервал из него построить нельзя: событие приходит *после* завершения запроса и
никак не даёт интервалу правильной вложенности относительно других интервалов, открытых в тот
момент. Поэтому интеграция — это [`QueryWrapper`](observers.ru.md#query-wrappers), механизм
hare для кода, выполняемого *вокруг* запроса: `instrument()` устанавливает её через
`Observers.wrap_queries()`, и она открывает интервал прямо перед вызовом драйвера и закрывает сразу
после, в задаче, которая отправила запрос, — точно так же, как это делают
`opentelemetry-instrumentation-dbapi`/`-sqlalchemy` и другие пакеты интеграции. В клиентах драйверов
ничего не подменяется: любой диалект и драйвер, в том числе сторонний, получает интервалы через ту
же обёртку.

**Связь с наблюдателем получается сама.** Интервал остаётся текущим всё время обёрнутого вызова — в
том числе в момент, когда изнутри него вызывается наблюдатель `QueryExecuted` (обычная функция),
поэтому наблюдатель, вызвавший `opentelemetry.trace.get_current_span()`, видит ровно тот интервал,
который открыт для наблюдаемого им запроса, — без какой-либо связи между ними.

## <a id="opentelemetryinstrumentor"></a>`OpenTelemetryInstrumentor`

```python
class OpenTelemetryInstrumentor:
    def __init__(
        self,
        tracer_provider: TracerProvider | None = None,
        meter_provider: MeterProvider | None = None,
        *,
        record_sql_statement: bool = True,
        query_spans: bool = True,
        pool_metrics: bool = True,
    ) -> None: ...
    def instrument(self) -> None: ...
    def uninstrument(self) -> None: ...
```

- `meter_provider`: MeterProvider метрик пула — глобальный (`opentelemetry.metrics.get_meter_provider()`),
  если не передан.
- `query_spans`/`pool_metrics`: запускает ли `instrument()` интервалы трассировки (`QuerySpans`) и метрики пула
  (`PoolMetricInstruments`). Метрики пула на время работы включают и `PoolMetrics` — замер каждого
  ожидания соединения; список метрик — в разделе [Метрики пула и здоровье](pool-health.ru.md#opentelemetry-metrics).

- `tracer_provider`: если не передан, используется глобальный (`opentelemetry.trace.get_tracer_provider()`)
  — настройте его (`TracerProvider` из SDK со своим экспортёром) до вызова `instrument()`.
- `record_sql_statement`: при `True` (по умолчанию) задаёт атрибут интервала `db.query.text` — текст
  SQL с местами для параметров (hare-orm никогда не вставляет в него значения, поэтому это безопасно по
  устройству). Передайте `False`, чтобы вообще его не записывать, если даже *устройство* запроса
  секретно.
- Повторные вызовы `instrument()`/`uninstrument()` у одного объекта ничего не меняют: второй вызов
  `instrument()` ничего не делает, а `uninstrument()` снимает обёртку. Одновременно можно установить
  несколько интеграций (с разными поставщиками трассировки). Импорт
  модуля ничего не включает — вы включаете интеграцию явно, как в любом настоящем пакете
  `opentelemetry-instrumentation-*`.

Каждый интервал содержит `db.system.name` (диалект — `sqlite`, `postgresql`, `clickhouse`; `other_sql` у
диалекта, который своего имени не задаёт) и, если не отключено,
`db.query.text`; это клиентский интервал (тип `CLIENT` в OpenTelemetry). Интервал неудачного запроса
получает стандартную для OpenTelemetry запись исключения и статус `ERROR` (из встроенного поведения
`record_exception`/`set_status_on_exception` у `start_as_current_span`) — ничего дополнительно
настраивать не нужно. Сообщение события исключения — это `str(exc)`, где есть текст SQL, но никогда нет
параметров запроса (см. [SQL ошибки базы данных](../errors/exceptions.ru.md#sqlerrormixin)). Однако собственный текст
ошибки базы сохраняется как есть: PostgreSQL указывает нарушающий ключ или строку в своей строке
`DETAIL` (`Key (email)=(...) already exists`, `Failing row contains (...)`), поэтому считайте, что
события исключений могут содержать данные строк.

Массовая загрузка PostgreSQL через `COPY` (`bulk_create(use_copy=True)`) получает свой интервал
`copy`. Текста SQL у неё нет, поэтому её `db.query.text` — `COPY <таблица> (<колонки>) FROM STDIN`,
но никогда не значения строк. Поток (`QuerySet.stream()`) получает интервал `stream` на всё время
перебора; текущим он становится, только пока читается очередная строка, поэтому интервалы, которые
ваш код открывает между строками, в него не вкладываются.

Намеренно **не** выводятся из текста SQL: `db.operation`/`db.sql.table`. Чтобы надёжно их извлекать,
нужен настоящий разборщик SQL, а польза невелика по сравнению с усилиями — это противоречило бы
принципу «не строить хрупких догадок», которому следует остальное наблюдение за запросами в hare-orm.

## <a id="combining-with-query-tags"></a>Вместе с метками в комментарии SQL

[`QueryTags`](query-tags.ru.md) и эта интеграция независимы — `QueryTags` намеренно не зависит от
`opentelemetry`. Если нужна метка, связывающая
комментарии SQL с текущей трассировкой, прочитайте её из текущего интервала сами:

```python
from opentelemetry import trace

from hare.instrumentation import QueryTags

span_context = trace.get_current_span().get_span_context()
with QueryTags.scope(trace_id=format(span_context.trace_id, "032x")):
    ...
```
