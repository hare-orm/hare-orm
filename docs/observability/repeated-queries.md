# Repeated ("N+1") query detection (`hare.contrib.repeated_queries`)

A runtime detector that counts identically shaped queries issued within one unit of work and
reports once a configurable threshold is crossed inside a sliding time window - an observer of
`QueryExecuted`, so it needs no changes to any backend.

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

`start()` adds the detector's observer process-wide (a second `start()` of the same detector does
nothing; `stop()` of one that isn't started does nothing either). Each detector is its own
instance with its own options and counts - two detectors with different thresholds can run side
by side.

Each query's already-parameter-bound SQL text is normalized into a "shape" key
(`RepeatedQueryDetector.shape_key()` - just whitespace normalization, no parsing) and counted per
shape, per unit of work. Once a shape's count *reaches* `threshold` within one `window_seconds`
window, `action` fires exactly once for that window - not on every call past the threshold - with
a `RepeatedQueryReport(shape_key, count, threshold, window_seconds, sql_sample, call_site)`.
`call_site` is the stack of the application code that issued the query that crossed the threshold,
ending at the innermost frame outside hare: the observer runs inline in the task that issued it.

`threshold` must be an `int` in `1..REPEATED_QUERY_MAX_THRESHOLD` (1 000 000) and `window_seconds` a
finite number in `(0, REPEATED_QUERY_MAX_WINDOW_SECONDS]` (one day) - anything else (`threshold=0`,
which could never fire, or a zero/negative window, which restarts the count on every query) raises
`ConfigurationError` when the detector is created. Both limits live in
`hare.contrib.repeated_queries.constants`. `action="warn"` (the default) logs via
`hare.core.log.logger.warning`; pass a callable instead (`Callable[[RepeatedQueryReport], None]`)
for custom routing - a metric, a structured log event. An exception it raises is logged and never
reaches the query - to fail a test on an N+1, assert on [`collect()`](#collect) instead. Any
other string is a `ConfigurationError`.

## Units of work {: #units-of-work }

hare-orm has no concept of "a request" of its own. The [FastAPI](../integrations/fastapi.md),
[Litestar](../integrations/litestar.md) and [Robyn](../integrations/robyn.md) integrations call
`RepeatedQueryDetector.reset_counts()` at the start of every request; elsewhere - a job runner, a
consumer loop - call it at the start of each unit of work, in the task that runs it. From then on
every detector counts from zero in that task and the tasks it starts, which add to the same counts.
Without any `reset_counts()` the first query of a task starts its counts, so the counts of a
long-lived task keep growing across what you'd consider separate units.

## Scoped collection: `collect()` {: #collect }

```python
RepeatedQueryDetector.shape_key(sql: str) -> str
RepeatedQueryDetector.collect() -> RepeatedQueryCollection

class RepeatedQueryCollection:  # `with` and `async with`
    total: int                                           # every counted query
    entries_by_shape_key: dict[str, RepeatedQueryEntry]
    def __iter__(self) -> Iterator[RepeatedQueryEntry]   # first-seen order
    def __len__(self) -> int                             # number of distinct shapes
    def count_for(self, sql_or_shape_key: str) -> int    # 0 for an unseen shape
    def repeated(self, threshold: int = 2) -> list[RepeatedQueryEntry]  # most frequent first

@dataclass
class RepeatedQueryEntry:
    shape_key: str
    count: int
    sql_sample: str   # the first SQL of this shape
    call_site: str    # stack of the first query of this shape, ending at the innermost non-hare frame
```

Counts queries per shape for one block of code - "how many identical queries did this request
make". Needs no detector and no `reset_counts()`, and works whether or not any detector is started.
Typical use is asserting that an endpoint has no N+1:

```python
async with RepeatedQueryDetector.collect() as collection:
    await client.get("/books")
assert collection.repeated(threshold=2) == [], [entry.sql_sample for entry in collection.repeated()]
```

`collect()` observes `QueryExecuted` with [`Observers.observing()`](observers.md#where-observers-live), so
counting is synchronous in the task that issued the query: every query of the block is already
counted by the time the block exits (no `wait_for_pending()` needed), and `call_site` points at the
real application frame. `with` and `async with` behave identically - entering and exiting do no
I/O. Semantics:

- **Concurrent tasks**, each with its own `collect()`, never see each other's queries.
- **Nested blocks**: a query inside an inner block is counted in the inner block *and* in every
  enclosing block.
- **Child tasks** created inside the block (`asyncio.create_task`, `gather` of coroutines, ...)
  inherit the context and are counted too - they run on behalf of the block. Queries they issue
  after the block has exited are ignored.
- One collection object can't be re-entered while active (`QueryError`); `repeated()` rejects
  `threshold < 1` with `QueryError`.
- Exiting in a different task than the one that entered (e.g. an async-generator pytest fixture
  whose teardown runs in another task) doesn't raise: the collection closes, stops counting, and
  its observer is removed from the exiting task's context.

`shape_key()` is the public normalization both modes use (idempotent, so `count_for()` accepts
either raw SQL or a shape key).

## The SQL-text heuristic {: #the-sql-text-heuristic }

The "shape" is the raw, already-parameter-bound SQL text - not any per-model structural key
hare-orm builds internally. This is a pragmatic heuristic, but it works well in practice: ordinary
filter values are bound as parameters rather than baked into the SQL text, on both the cached and
uncached query-building paths, so two calls that only differ by filter value collapse to the same
shape - including the currently active timezone name used to build an
`EXTRACT(... AT TIME ZONE 'zone')`-style `__year`/`__month`/etc lookup, which (like every other
value) reaches the final SQL text as a bound parameter, so two otherwise identical zone-aware
lookups issued under two different configured timezones are intentionally the *same* shape here.
A bulk `COPY` is counted like any other statement, under its `COPY <table> (<columns>) FROM STDIN`
text - one per batch, never N+1-shaped in practice.
