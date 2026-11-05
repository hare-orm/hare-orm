//! A statement retains the serialized connection through preparation, execution and decoding.

use futures_util::TryStreamExt;
use std::pin::pin;
use std::sync::Arc;
use tokio::sync::MutexGuard;
use tokio_postgres::{Row, RowStream};

use crate::pg::client::StatementCaching;
use crate::pg::client::{CancelQueryOnDrop, PinnedConnectionState};
use crate::pg::error::DriverError;
use crate::pg::one_round_trip::{binds_in_one_round_trip, get_result_columns, typed_parameters};
use crate::pg::parameter_values::ParameterValues;
use crate::pg::result_columns::ResultColumns;
use crate::pg::result_data::ResultData;
use crate::pg::timed_manager::Object;
use crate::pg::value::Value;

pub(crate) struct PgStatementLease<'a> {
    // Register cancellation before releasing the connection lock when an operation is abandoned.
    cancellation: CancelQueryOnDrop,
    connection: MutexGuard<'a, Option<Object>>,
}

impl<'a> PgStatementLease<'a> {
    pub(crate) fn new(connection: MutexGuard<'a, Option<Object>>, state: &PinnedConnectionState) -> Self {
        let token = connection.as_ref().expect("acquired connection").cancel_token();
        Self { cancellation: CancelQueryOnDrop::new(token, state), connection }
    }

    pub(crate) fn client(&self) -> &Object {
        self.connection.as_ref().expect("statement retains its connection")
    }

    pub(crate) fn client_mut(&mut self) -> &mut Object {
        self.connection.as_mut().expect("statement retains its connection")
    }

    pub(crate) fn finish<T>(&mut self, outcome: Result<T, DriverError>) -> Result<T, DriverError> {
        self.cancellation.finish(outcome)
    }

    pub(crate) async fn execute(
        &mut self,
        sql: &str,
        params: &[Value],
        caching: &StatementCaching,
    ) -> Result<u64, DriverError> {
        let client = self.client();
        let outcome = async {
            if binds_in_one_round_trip(caching, params) {
                return client.execute_typed(sql, &typed_parameters(params)).await.map_err(DriverError::from);
            }
            let bound = ParameterValues::as_sql_refs(params);
            caching
                .run(client, sql, params, |statement| async move { client.execute(&statement, &bound).await })
                .await
                .map(|(_, affected)| affected)
        }
        .await;
        self.finish(outcome)
    }

    /// Shared raw/described path, reading every returned row through `CommandComplete`.
    pub(crate) async fn fetch(
        &mut self,
        sql: &str,
        params: &[Value],
        caching: &StatementCaching,
    ) -> Result<(Arc<ResultColumns>, ResultData), DriverError> {
        let client = self.client();
        let outcome = async {
            let (columns, rows, row_count) = if binds_in_one_round_trip(caching, params) {
                let stream = client.query_typed_raw(sql, typed_parameters(params)).await?;
                let (rows, row_count) = Self::read_stream(stream).await?;
                let columns = get_result_columns(client, sql, params, &rows).await?;
                (columns, rows, row_count)
            } else {
                let bound = ParameterValues::as_sql_refs(params);
                let (statement, (rows, row_count)) = caching
                    .run(client, sql, params, |statement| async move {
                        let stream = client.query_raw(&statement, bound).await?;
                        Self::read_stream(stream).await
                    })
                    .await?;
                (ResultColumns::of_statement(&statement), rows, row_count)
            };
            Ok((columns, ResultData::read(client, rows, row_count).await?))
        }
        .await;
        self.finish(outcome)
    }

    /// Consume through `CommandComplete` to retain the affected-row count alongside the rows.
    async fn read_stream(stream: RowStream) -> Result<(Vec<Row>, Option<u64>), tokio_postgres::Error> {
        let mut stream = pin!(stream);
        let mut rows = Vec::new();
        while let Some(row) = stream.as_mut().try_next().await? {
            rows.push(row);
        }
        Ok((rows, stream.rows_affected()))
    }
}
