use crate::asyncio::completion::future_into_py;
use crate::pg::error::DriverError;
use crate::pg::first_row::FirstRow;
use crate::pg::result::PgResult;
use crate::pg::result_columns::ResultColumns;
use crate::pg::result_data::ResultData;
use crate::pg::value::Value;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use tokio::sync::Mutex;

use super::cursor_rows::CursorRows;
use super::{stream_exhausted, tx_finished, Transaction};
use pyo3::prelude::*;

/// How many rows `__anext__()` fetches off the cursor at a time.
const ITERATION_FETCH_SIZE: usize = 1000;

/// State backing one open `RowStreamIterator`.
pub(super) struct OpenRowStream {
    pub(super) rows: CursorRows,
    pub(super) columns: Arc<ResultColumns>,
    pub(super) connection_poisoned: Arc<AtomicBool>,
    /// An error met after rows already returned by `fetch_many()` - raised by the next fetch.
    pub(super) pending_error: Option<DriverError>,
}

impl OpenRowStream {
    /// The next row's values - the next `fetch_size` rows fetched first when none is left; None at the
    /// end of the stream.
    async fn next_values(&mut self, fetch_size: usize) -> Option<Result<Vec<Value>, DriverError>> {
        if let Some(error) = self.pending_error.take() {
            return Some(Err(error));
        }
        self.rows.next_values(fetch_size).await
    }
}

/// One `RowStreamIterator`'s state, shared with the `Transaction` it was opened on so that ending
/// the transaction can drop its cursor's rows even while Python still holds the iterator.
pub(super) struct RowStreamSlot {
    /// `None` once the stream is exhausted, has raised, or was closed.
    pub(super) state: Mutex<Option<OpenRowStream>>,
    /// Set by `close()`: a fetch in flight while it ran drops the stream once it finishes.
    pub(super) close_requested: AtomicBool,
    /// Set when the owning transaction ended while the stream was still open.
    pub(super) closed_by_transaction_end: AtomicBool,
}

impl RowStreamSlot {
    /// Drops the stream now, or - while a fetch holds it - as soon as
    /// that fetch finishes.
    pub(super) fn close(&self, by_transaction_end: bool) {
        if by_transaction_end {
            self.closed_by_transaction_end.store(true, Ordering::Release);
        }
        self.close_requested.store(true, Ordering::Release);
        if let Ok(mut state) = self.state.try_lock() {
            *state = None;
        }
    }

    /// What a fetch on a stream that is no longer open raises.
    fn closed_error(&self) -> PyErr {
        if self.closed_by_transaction_end.load(Ordering::Acquire) {
            tx_finished()
        } else {
            stream_exhausted()
        }
    }
}

/// `QuerySet.stream()`'s `rust_pg` row source - implements Python's async-iterator protocol
/// directly. Once the stream is exhausted, has raised or was closed, further `__anext__` calls
/// raise `StopAsyncIteration` (`TransactionFinishedError` when the transaction ended first)
/// rather than re-polling a finished stream.
#[pyclass]
pub struct RowStreamIterator {
    pub(super) slot: Arc<RowStreamSlot>,
}

#[pymethods]
impl RowStreamIterator {
    fn __aiter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    /// Drops the stream right away, without fetching the remaining rows - its cursor ends with the
    /// transaction. Idempotent.
    fn close(&self) {
        self.slot.close(false);
    }

    fn __anext__<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let slot = self.slot.clone();
        future_into_py(py, async move {
            let mut guard = slot.state.lock().await;
            if slot.close_requested.load(Ordering::Acquire) {
                *guard = None;
            }
            let Some(state) = guard.as_mut() else {
                return Err(slot.closed_error());
            };
            let next_values = state.next_values(ITERATION_FETCH_SIZE).await;
            let columns = state.columns.clone();
            let connection_poisoned = state.connection_poisoned.clone();
            // The stream is unusable after an error or its end - drop it so a later __anext__()
            // gets a clean StopAsyncIteration instead of polling a finished stream again. A
            // close() that arrived while this fetch held the stream drops it now as well.
            if !matches!(next_values, Some(Ok(_))) || slot.close_requested.load(Ordering::Acquire) {
                *guard = None;
            }
            drop(guard);
            match next_values {
                Some(Ok(values)) => Ok(FirstRow { columns, values: Some(values) }),
                Some(Err(error)) => Transaction::note_outcome(&connection_poisoned, Err(error)),
                None => Err(stream_exhausted()),
            }
        })
    }

    /// Up to `size` rows - fewer only at the end of the stream, none once it is exhausted. An error
    /// met after some rows is raised by the next call, so those rows come first, as one by one.
    fn fetch_many<'py>(&self, py: Python<'py>, size: usize) -> PyResult<Bound<'py, PyAny>> {
        let slot = self.slot.clone();
        future_into_py(py, async move {
            let mut guard = slot.state.lock().await;
            if slot.close_requested.load(Ordering::Acquire) {
                *guard = None;
            }
            let Some(state) = guard.as_mut() else {
                if slot.closed_by_transaction_end.load(Ordering::Acquire) {
                    return Err(tx_finished());
                }
                return Ok(PgResult::new(ResultColumns::none(), ResultData::Values(Vec::new())));
            };
            let mut batch = Vec::with_capacity(size.clamp(1, 1024));
            let mut failure = None;
            let mut ended = false;
            while batch.len() < size.max(1) {
                match state.next_values(size.max(1) - batch.len()).await {
                    Some(Ok(values)) => batch.push(values),
                    Some(Err(error)) if batch.is_empty() => {
                        failure = Some(error);
                        break;
                    }
                    Some(Err(error)) => {
                        state.pending_error = Some(error);
                        break;
                    }
                    None => {
                        ended = true;
                        break;
                    }
                }
            }
            let columns = state.columns.clone();
            let connection_poisoned = state.connection_poisoned.clone();
            if failure.is_some() || ended || slot.close_requested.load(Ordering::Acquire) {
                *guard = None;
            }
            drop(guard);
            if let Some(error) = failure {
                return Transaction::note_outcome(&connection_poisoned, Err(error));
            }
            Ok(PgResult::new(columns, ResultData::Values(batch)))
        })
    }
}
