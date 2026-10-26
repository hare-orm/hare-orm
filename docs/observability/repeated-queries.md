# N+1 query detection

A runtime detector that counts identically shaped queries issued within one unit of work and reports once
a configurable threshold is crossed inside a sliding time window — an observer of `QueryExecuted`, so it
needs no changes to any dialect.

```python
class RepeatedQueryDetector:
    def __init__(
        self, *, threshold: int = 10, window_seconds: float = 1.0, action: ReportAction | str = "warn"
    ) -> None: ...

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def get_counts(self) -> dict[str, ShapeStats]: ...

    @classmethod
    def reset_counts(cls) -> None: ...
```

A detector for the whole process, or for one block:

```python
from hare.contrib.repeated_queries import RepeatedQueryDetector

detector = RepeatedQueryDetector(threshold=10, window_seconds=1.0, action="warn")
await detector.start()  # once, at process/app startup
...
await detector.stop()

# or for a block:
async with RepeatedQueryDetector(threshold=10, window_seconds=1.0):
    ...
```

```python
async def handle_job():
    RepeatedQueryDetector.reset_counts()  # a new unit of work - see below
    ...
```

## <a id="detector"></a>Running a detector

`start()` adds the detector's observer process-wide (a second `start()` of the same detector does
nothing; `stop()` of one that isn't started does nothing either). Each detector is its own instance with
its own options and counts — two detectors with different thresholds can run side by side.
`detector.get_counts()` is its counts in the current unit of work: shape key to
`ShapeStats(sql_sample, window_start, count)` of the shape's current window.

## <a id="shapes"></a>Shapes and reports

Each query's already-parameter-bound SQL text is normalized into a "shape" key
(`RepeatedQueryDetector.shape_key()` — just whitespace normalization, no parsing) and counted per shape,
per unit of work. The values are placeholders, so the same query with other values is one shape — but an
`__in` list of another length has as many placeholders more or less, and is another shape
(`id IN ($1, $2)` and `id IN ($1, $2, $3)`); a list long enough to be bound as one array (`= ANY($1)` on
PostgreSQL) is one shape at any length. Once a shape's count *reaches* `threshold` within one
`window_seconds` window, `action` fires exactly once for that window — not on every call past the
threshold — with a
`RepeatedQueryReport(shape_key, count, threshold, window_seconds, sql_sample, call_site)`. `call_site` is
the stack of the application code that issued the query that crossed the threshold, ending at the
innermost frame outside hare: the observer runs inline in the task that issued it.

## <a id="options"></a>Options

`threshold` is how many queries of one shape within `window_seconds` are repeated; a shape below the
threshold that long after its first query starts counting fresh. `action="warn"` (the default) logs via
`hare.core.log.logger.warning`; pass a callable instead (`Callable[[RepeatedQueryReport], None]`) for
custom routing — a metric, a structured log event. An exception it raises is logged and never reaches the
query — to fail a test on an N+1, assert on [`collect()`](#collect) instead. Any other string is a
`ConfigurationError`.

## <a id="units-of-work"></a>Units of work

hare has no concept of "a request" of its own. The [FastAPI](../integrations/fastapi.md),
[Litestar](../integrations/litestar.md) and [Robyn](../integrations/robyn.md) integrations call
`RepeatedQueryDetector.reset_counts()` at the start of every request; elsewhere — a job runner, a
consumer loop — call it at the start of each unit of work, in the task that runs it. From then on every
detector counts from zero in that task and the tasks it starts, which add to the same counts. Without any
`reset_counts()` the first query of a task starts its counts, so the counts of a long-lived task keep
growing across what you'd consider separate units.

## <a id="collect"></a>Scoped collection: `collect()`

```python
class RepeatedQueryDetector:
    @staticmethod
    def shape_key(sql: str) -> str: ...

    @staticmethod
    def collect() -> RepeatedQueryCollection: ...

# `with` and `async with`
class RepeatedQueryCollection:
    # every counted query
    total: int
    entries_by_shape_key: dict[str, RepeatedQueryEntry]

    # first-seen order
    def __iter__(self) -> Iterator[RepeatedQueryEntry]: ...

    # the number of distinct shapes
    def __len__(self) -> int: ...

    # 0 for an unseen shape
    def count_for(self, sql_or_shape_key: str) -> int: ...

    # the most frequent first
    def repeated(self, threshold: int = 2) -> list[RepeatedQueryEntry]: ...


@dataclass
class RepeatedQueryEntry:
    shape_key: str
    count: int
    # the first SQL of this shape
    sql_sample: str
    # the stack of the first query of this shape, ending at the innermost frame outside hare
    call_site: str
```

Counts queries per shape for one block of code — "how many identical queries did this request make".
Needs no detector and no `reset_counts()`, and works whether or not any detector is started. Typical use
is asserting that an endpoint has no N+1:

```python
async with RepeatedQueryDetector.collect() as collection:
    await client.get("/books")
assert collection.repeated(threshold=2) == [], [entry.sql_sample for entry in collection.repeated()]
```

`collect()` observes `QueryExecuted` with [`Observers.observing()`](observers.md#where-observers-live),
so counting is synchronous in the task that issued the query: every query of the block is already counted
by the time the block exits (no `wait_for_pending()` needed), and `call_site` points at the real
application frame. `with` and `async with` behave identically — entering and exiting do no I/O.
Semantics:

- **Concurrent tasks**, each with its own `collect()`, never see each other's queries.
- **Nested blocks**: a query inside an inner block is counted in the inner block *and* in every enclosing
  block.
- **Child tasks** created inside the block (`asyncio.create_task`, `gather` of coroutines, ...) inherit
  the context and are counted too — they run on behalf of the block. Queries they issue after the block
  has exited are ignored.
- One collection object can't be re-entered while active (`QueryError`); `repeated()` rejects
  `threshold < 1` with `QueryError`.
- Exiting in a different task than the one that entered (e.g. an async-generator pytest fixture whose
  teardown runs in another task) doesn't raise: the collection closes, stops counting, and its observer
  is removed from the exiting task's context.

`shape_key()` is the public normalization both modes use (idempotent, so `count_for()` accepts either raw
SQL or a shape key).

## <a id="the-sql-text-heuristic"></a>The SQL-text heuristic

The "shape" is the raw, already-parameter-bound SQL text — not any per-model structural key hare builds
internally. This is a pragmatic heuristic, but it works well in practice: ordinary filter values are
bound as parameters rather than baked into the SQL text, on both the cached and uncached query-building
paths, so two calls that only differ by filter value collapse to the same shape — including the currently
active timezone name used to build an `EXTRACT(... AT TIME ZONE 'zone')`-style `__year`/`__month`/etc
lookup, which (like every other value) reaches the final SQL text as a bound parameter, so two otherwise
identical zone-aware lookups issued under two different configured timezones are intentionally the *same*
shape here. A bulk `COPY` is counted like any other statement, under its
`COPY <table> (<columns>) FROM STDIN` text — one per batch, never N+1-shaped in practice.
