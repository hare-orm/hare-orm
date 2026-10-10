# Query and value assertions

Checks a test makes beyond the values it reads: how many queries a block of code runs, and matchers
that compare a value by a rule instead of by equality.

## <a id="assert-num-queries"></a>`assert_query_count()` / `capture_queries()` — catch N+1 regressions

```python
async def capture_queries(using: str | DatabaseClient | None = None) -> AsyncGenerator[QueryCounter]

async def assert_query_count(expected: int, *, using: str | DatabaseClient | None = None) -> AsyncGenerator[QueryCounter]
```

```python
async with assert_query_count(1):
    await Event.objects.all().select_related("tournament")
```

`assert_query_count` fails with an `AssertionError` listing every captured query's SQL text if the
block runs anything other than exactly `expected` queries — the direct way to pin down a missing
`select_related()`/`prefetch_related()` as a test failure instead of discovering it in production.
`capture_queries` is the lower-level primitive it's built on, for when you want the count/query list
without asserting on it:

```python
async with capture_queries() as counter:
    await Event.objects.all().select_related("tournament")
assert counter.count == 1
```

`QueryCounter` (`connection_alias: str`, `count: int`, `queries: list[str]`) updates live as the block runs, not only once it
exits. Every statement counts exactly once — model loads, `values()`/`values_list()`,
`aggregate()`, `count()`/`exists()`, writes, raw SQL, each `stream()` and each
`bulk_create(use_copy=True)` batch (recorded as `COPY <table> (<columns>) FROM STDIN`) — even where
one client method internally delegates to another.

## <a id="value-matchers"></a>Value matchers (`hare.contrib.test.conditions`)

Not re-exported from the top-level `hare.contrib.test` package — import from the submodule
directly:

```python
from hare.contrib.test.conditions import In, NotEQ, NotIn
```

Usable on the right-hand side of an `==` comparison, e.g. inside a dict compared against real data
in an assertion:

| Class | Matches |
|---|---|
| `NotEQ(value)` | Anything not equal to `value`. |
| `In(*values)` | Anything that is one of `values`. |
| `NotIn(*values)` | Anything that is none of `values`. |

```python
assert response.json() == {"id": In(*known_ids), "status": NotEQ("deleted")}
```
