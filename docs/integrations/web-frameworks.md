# Web frameworks

The integrations for [Litestar](litestar.md), [FastAPI](fastapi.md) and [Robyn](robyn.md) share how a
request runs in a transaction and the health routes they add; FastAPI and Robyn also the schemas of a
response. This page describes what they share; each framework's page says how to plug hare into it.

## <a id="atomic-requests"></a>A transaction per request: `atomic_requests`

`atomic_requests=True` runs each HTTP request in a transaction on the default connection; a sequence of
connection aliases opens one on each of them. The transaction:

- commits when the response has a status below 500 — before the response is sent, so a failed commit is
  still answered with an error; the body of a streaming response (Litestar, FastAPI) is sent after the
  commit;
- rolls back on a response with a status of 500 or more, and on an exception, handled or not;
- runs the `Transactions.on_commit()` callbacks after the commit, as usual.

With several connections the transactions commit one after another; a failure to commit one rolls back
those not committed yet, not those already committed. An alias the configuration doesn't have stops the
application from starting. So does a connection that runs no transactions — a ClickHouse one opened
without `transactions=true` — with `ConfigurationError` once its database answers, instead of failing
every request.

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
