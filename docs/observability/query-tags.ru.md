# Метки в комментариях SQL

`hare.instrumentation.QueryTags` помечает каждую команду SQL, выполненную внутри блока, комментарием в
конце в стиле [sqlcommenter](https://google.github.io/sqlcommenter/) — чтобы по тексту SQL из
`pg_stat_statements`/`auto_explain`/журнала медленных запросов найти место в приложении, откуда он
пришёл, без зависимости hare от какой-либо системы трассировки.

```python
class QueryTags:
    @classmethod
    def scope(cls, **tags: str) -> AbstractContextManager[None]: ...

    @classmethod
    def set(cls, tags: dict[str, str]) -> contextvars.Token[dict[str, str] | None]: ...

    @classmethod
    def reset(cls, token: contextvars.Token[dict[str, str] | None]) -> None: ...
```

Ночная задача, которая напоминает о просроченных выдачах, помечает свои запросы:

```python
from hare.instrumentation import QueryTags

with QueryTags.scope(application="ops", job="remind_overdue_loans"):
    await Loan.objects.filter(status="overdue").all()
    # SQL здесь заканчивается так: ... /*application='ops',job='remind_overdue_loans'*/
```

## <a id="the-comment"></a>Комментарий

- Комментарий **добавляется в конец**, никогда в начало: комментарий в начале мешал бы кэшу запросов
  PostgreSQL, который опирается на начало текста, и путал бы инструменты, читающие только первые N
  символов запроса.
- Ключи и значения кодируются как в URL — ключ должен быть непустой строкой, а значение не None, иначе
  `ConfigurationError`; ключи сортируются, поэтому один и тот же набор меток всегда даёт один и тот же
  текст комментария, в каком бы порядке его ни передали.
- Без активных меток SQL возвращается без изменений (тот же объект) — в обычном случае без меток
  накладных расходов нет.

## <a id="nesting"></a>Вложенные блоки

Вложенные блоки не сливаются: блок `QueryTags.scope()` внутри другого добавляет к своим запросам только
свои метки — метки внешнего блока снова действуют после выхода из внутреннего.

`QueryTags.set(tags)`/`QueryTags.reset(token)` — низкоуровневые примитивы, на которых построен
`QueryTags.scope()`, — для случаев, когда один блок `with` не может охватить всё время жизни меток
(например, через границу `await`, охватывающую несколько вызовов обработчиков).

## <a id="tagged-statements"></a>Какие команды получают комментарий

- Каждый вызов, выполняющий запрос, включая DDL-сценарий через `execute_script()`: сценарий из нескольких
  запросов получает ровно один комментарий в конце, а не по одному на запрос.
- Массовая загрузка `COPY` в PostgreSQL (`bulk_create(use_copy=True)`): на `asyncpg`, который сам пишет
  команду, комментарий идёт после `WHERE TRUE`, которому отвечает каждая строка.
- `QueryExecuted.sql` и `QueryCall.sql` содержат уже помеченный текст: метки добавляются до того, как
  запрос уходит в драйвер.
