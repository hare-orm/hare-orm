# Web frameworks

The integrations for [Litestar](litestar.md), [FastAPI](fastapi.md) and [Robyn](robyn.md) share how a
request runs in a transaction and the health routes they add; FastAPI and Robyn also the schemas of a
response. This page describes what they share; each framework's page says how to plug hare into it.

If application startup or lifespan work fails and closing its Hare context also fails, both errors are
preserved in an exception group. A borrowed context remains under its caller's control and is not closed
by the integration. Robyn applies the same error handling to its startup and shutdown callbacks.

## <a id="atomic-requests"></a>A transaction per request: `atomic_requests`

`atomic_requests=True` runs each HTTP request in one automatically selected transaction. Selection tries
in order: the only configured connection, the common connection of all registered models, then the alias
`default`. An ambiguous configuration raises `QueryError`. An iterable of connection aliases opens a
transaction on each named connection. Its iteration order is captured when the application is constructed;
lists, tuples and one-shot iterators are supported, but a single string is not a collection of aliases.
The transaction:

- commits when the response has a status below 500 — before the response is sent, so a failed commit is
  still answered with an error; the body of a streaming response (Litestar, FastAPI) is sent after the
  commit;
- rolls back on a response with a status of 500 or more, and on an exception, handled or not;
- runs the `Transactions.on_commit()` callbacks after the commit, as usual.

With several connections the transactions commit in reverse configuration order (the last opened first);
for `atomic_requests=["first", "second"]`, `second` commits before `first`. A failure to commit one rolls back
those not committed yet, not those already committed. An alias the configuration doesn't have stops the
application from starting. So does a connection that runs no transactions — a ClickHouse one opened
without `transactions=true` — with `ConfigurationError` once its database answers, instead of failing
every request.

Request shutdown waits for every transaction exit even if one fails or the request is cancelled
repeatedly. Cancellation stops commits which have not started; an already submitted commit can still
succeed. A cleanup error does not imply rollback, and independent connections are not one atomic
transaction. If cleanup also fails while a request error is propagating, both errors are preserved in
an exception group. Streaming and background work run after this transaction boundary.

What runs without the transaction:

| Framework | Without the transaction |
|---|---|
| Litestar | a route handler with `opt={SKIP_TRANSACTION_OPT_KEY: True}`; a WebSocket connection |
| FastAPI | a route whose endpoint is marked `RequestTransaction.skip`; a WebSocket connection |
| Robyn | a handler marked `RequestTransaction.skip`; a const route (`const=True`, Robyn's own `/openapi.json` and `/docs` among them), which the server runs once as it starts, before the hare context opens |

## <a id="response-schemas"></a>Response schemas: `response_model`

A FastAPI or Robyn route returns hare models with its `response_model`: a schema of one row with
`from_attributes=True` for a row or a list (`list[BookSchema]`), and for a page of rows the page's schema
— `PageSchema[BookSchema]` (`ScrollPageSchema`, `CursorPageSchema` for the other pages). The rows of a
page are in `result`. Litestar returns models through `HareDTO` instead — see
[Responses: `HareDTO`](litestar.md#haredto).

## <a id="health-routes"></a>Health routes: `health_routes`

`health_routes=HealthRoutes(HealthCheck())` adds a readiness route and a liveness route:

- `/health/ready` — the connections pinged and judged by the `HealthCheck`: 200, `degraded_status_code`
  for a degraded application, 503 for an unhealthy one;
- `/health/live` — 200 while the process answers, the database untouched.

Neither runs in the request's transaction or appears in the OpenAPI schema. The paths, the degraded
status and the details of the answer are `HealthRoutes`'s arguments — see [Pool metrics and
health](../observability/pool-health.md#liveness-and-readiness).
