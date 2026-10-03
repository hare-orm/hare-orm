# SQL comment tagging (`hare.instrumentation.QueryTags`)

Tags every SQL statement executed inside a block with a trailing,
[sqlcommenter-style](https://google.github.io/sqlcommenter/) SQL comment - the motivating use case is
correlating raw SQL text seen by `pg_stat_statements`/`auto_explain`/a slow-query log back to the
application call site that issued it, without hare-orm depending on any particular
tracing/APM library.

```python
from hare.instrumentation import QueryTags

with QueryTags.scope(application="ops", job="sync_pending_widgets"):
    await Widget.objects.filter(status="pending").all()
    # SQL executed here ends with: ... /*application='ops',job='sync_pending_widgets'*/
```

- The comment is **appended**, never prepended - a leading comment would perturb Postgres's own
  prefix-based statement-cache behavior and confuse tools that read only the first N characters of
  a query.
- Values are URL-encoded; keys are sorted, so the same tag set always produces the same comment
  text regardless of the order you passed them in.
- Nesting is exact, not merged: a `QueryTags.scope()` block inside another sees only its own tags
  for the statements it wraps - the outer block's tags are back in effect once the inner block
  exits.
- With no tags active, `sql` is returned completely unchanged (the same object) - zero overhead
  for the common case of no tagging.
- Applies to every query-executing call, including a DDL script run through `execute_script()` -
  a multi-statement script gets exactly one trailing comment for the whole script, not one per
  statement.
- `QueryExecuted.sql` and `QueryCall.sql` carry the already-tagged SQL text, since tagging happens
  before the query is dispatched to the driver.

`QueryTags.set(tags)`/`QueryTags.reset(token)` are the lower-level primitives `QueryTags.scope()`
itself is built on, for a case where a single `with` block can't span the tags' intended lifetime
(e.g. across an `await` boundary spanning multiple callback invocations).
