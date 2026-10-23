//! `CursorRows` - the rows of a `QuerySet.stream()` read off a server-side cursor a batch at a time.

use crate::pg::client::decode_rows;
use crate::pg::error::DriverError;
use crate::pg::timed_manager::Object;
use crate::pg::value::Value;
use std::collections::VecDeque;
use std::sync::Arc;
use tokio::sync::Mutex;
use tokio_postgres::Statement;

/// The rows of a stream: a cursor its transaction declared, fetched a batch at a time. Each FETCH is
/// read to its end, so a statement the transaction runs between two batches - from the loop's body, or
/// from a task sharing the transaction - goes out at once; the rows of a portal read as they arrived
/// held the connection until the last of them was read.
pub(super) struct CursorRows {
    /// The cursor's name, unique within its transaction.
    name: String,
    /// The transaction's connection - None once the transaction ended.
    connection: Arc<Mutex<Option<Object>>>,
    /// The rows fetched and not handed out yet.
    rows: VecDeque<Vec<Value>>,
    /// Whether the cursor has no more rows.
    exhausted: bool,
    /// The FETCH prepared for the stream and the number of rows it fetches - prepared once, each
    /// batch then one round trip; prepared again only for a batch of another size.
    fetch_statement: Option<(usize, Statement)>,
}

impl CursorRows {
    pub(super) fn new(name: String, connection: Arc<Mutex<Option<Object>>>) -> Self {
        CursorRows { name, connection, rows: VecDeque::new(), exhausted: false, fetch_statement: None }
    }

    /// The next row's values - fetching the next `fetch_size` rows first when none is left; None at
    /// the end of the rows.
    pub(super) async fn next_values(&mut self, fetch_size: usize) -> Option<Result<Vec<Value>, DriverError>> {
        if self.rows.is_empty() && !self.exhausted {
            if let Err(error) = self.fetch(fetch_size.max(1)).await {
                self.exhausted = true;
                return Some(Err(error));
            }
        }
        self.rows.pop_front().map(Ok)
    }

    /// Fetches the next `fetch_size` rows of the cursor - fewer only at its end.
    async fn fetch(&mut self, fetch_size: usize) -> Result<(), DriverError> {
        let guard = self.connection.lock().await;
        let client =
            guard.as_ref().ok_or_else(|| DriverError::Query("the stream's transaction already ended".to_string()))?;
        let statement = match &self.fetch_statement {
            Some((prepared_size, statement)) if *prepared_size == fetch_size => statement.clone(),
            _ => {
                let fetch_sql = format!("FETCH FORWARD {fetch_size} FROM \"{}\"", self.name);
                let statement = client.prepare(fetch_sql.as_str()).await.map_err(DriverError::from)?;
                self.fetch_statement = Some((fetch_size, statement.clone()));
                statement
            }
        };
        let rows = client.query(&statement, &[]).await.map_err(DriverError::from)?;
        if rows.len() < fetch_size {
            self.exhausted = true;
        }
        self.rows.extend(decode_rows(client, &rows).await?);
        Ok(())
    }
}
