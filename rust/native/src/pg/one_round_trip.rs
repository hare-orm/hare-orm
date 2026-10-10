//! Statements sent with their values in one round trip - an unnamed statement parsed, bound, described
//! and run at once - on a connection caching no statements (`statement_cache_size=0`, e.g. behind
//! PgBouncer in transaction pooling), where preparing first would take a round trip of its own for
//! every statement.
//!
//! Only values each carrying a type of its own (`Value::pg_type()`), or NULL, go this way: the others
//! are written for the type the server infers for their parameter (`Value::encode`), which only a
//! prepared statement's description tells.

use std::sync::Arc;

use tokio_postgres::types::{ToSql, Type};
use tokio_postgres::Row;

use crate::pg::client::{unspecified_type, StatementCaching};
use crate::pg::error::DriverError;
use crate::pg::result_columns::ResultColumns;
use crate::pg::value::Value;

/// Whether `params` go out with their statement in one round trip on a connection with `caching`.
pub(crate) fn binds_in_one_round_trip(caching: &StatementCaching, params: &[Value]) -> bool {
    !caching.enabled && params.iter().all(|value| matches!(value, Value::Null) || value.pg_type().is_some())
}

/// `params` with the type each is bound as - its own, the unspecified type for NULL.
pub(crate) fn typed_parameters(params: &[Value]) -> Vec<(&(dyn ToSql + Sync), Type)> {
    params
        .iter()
        .map(|value| (value as &(dyn ToSql + Sync), value.pg_type().unwrap_or_else(unspecified_type)))
        .collect()
}

/// The columns of a result read in one round trip: those of its first row, or - a result of no row -
/// those a description of the statement gives, a round trip of its own.
pub(crate) async fn get_result_columns(
    client: &tokio_postgres::Client,
    sql: &str,
    params: &[Value],
    rows: &[Row],
) -> Result<Arc<ResultColumns>, DriverError> {
    if let Some(first_row) = rows.first() {
        return Ok(ResultColumns::of_columns(first_row.columns()));
    }
    let types: Vec<Type> = params.iter().map(|value| value.pg_type().unwrap_or_else(unspecified_type)).collect();
    let statement = client.prepare_typed(sql, &types).await?;
    Ok(ResultColumns::of_statement(&statement))
}
