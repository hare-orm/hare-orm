# SQL comment tags

`hare.instrumentation.QueryTags` tags every SQL statement executed inside a block with a trailing,
[sqlcommenter-style](https://google.github.io/sqlcommenter/) SQL comment — for tracing the SQL text seen
by `pg_stat_statements`/`auto_explain`/a slow-query log back to the application call site that issued it,
without hare depending on any particular tracing/APM library.

```python
class QueryTags:
    @classmethod
    def scope(cls, **tags: str) -> AbstractContextManager[None]: ...

    @classmethod
    def set(cls, tags: dict[str, str]) -> contextvars.Token[dict[str, str] | None]: ...

    @classmethod
    def reset(cls, token: contextvars.Token[dict[str, str] | None]) -> None: ...
```

A nightly job sending reminders for overdue loans tags its queries:

```python
from hare.instrumentation import QueryTags

with QueryTags.scope(application="ops", job="remind_overdue_loans"):
    await Loan.objects.filter(status="overdue").all()
    # SQL executed here ends with: ... /*application='ops',job='remind_overdue_loans'*/
```

## <a id="the-comment"></a>The comment

- The comment is **appended**, never prepended — a leading comment would perturb PostgreSQL's
  prefix-based statement-cache behavior and confuse tools that read only the first N characters of a
  query.
- Keys and values are URL-encoded — a key is a non-empty string and a value isn't None, else
  `ConfigurationError`; keys are sorted, so the same tag set always produces the same comment text
  regardless of the order you passed them in.
- With no tags active, the SQL is returned completely unchanged (the same object) — no overhead for the
  common case of no tagging.

## <a id="nesting"></a>Nesting

Nesting is exact, not merged: a `QueryTags.scope()` block inside another sees only its own tags for the
statements it wraps — the outer block's tags are back in effect once the inner block exits.

`QueryTags.set(tags)`/`QueryTags.reset(token)` are the lower-level primitives `QueryTags.scope()` itself
is built on, for a case where a single `with` block can't span the tags' intended lifetime (e.g. across
an `await` boundary spanning multiple callback invocations).

## <a id="tagged-statements"></a>Which statements get the comment

- Every query-executing call, including a DDL script run through `execute_script()` — a multi-statement
  script gets exactly one trailing comment for the whole script, not one per statement.
- A PostgreSQL bulk `COPY` (`bulk_create(use_copy=True)`): on `asyncpg`, which writes the statement
  itself, the comment follows a `WHERE TRUE` every row meets.
- `QueryExecuted.sql` and `QueryCall.sql` carry the already-tagged SQL text, since tagging happens before
  the query is dispatched to the driver.
