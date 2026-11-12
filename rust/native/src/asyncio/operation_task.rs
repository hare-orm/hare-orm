//! The asyncio future owns its deferred native task; the start queue refers to it weakly.
//! Closing a loop before its start callback runs must not root a task or the loop.

use std::future::Future;
use std::pin::Pin;
use std::sync::Mutex;

use futures_util::future::AbortHandle;
use pyo3::intern;
use pyo3::prelude::*;

type QueryTask = Pin<Box<dyn Future<Output = ()> + Send>>;

#[pyclass(frozen, weakref)]
pub struct OperationTask {
    abort: AbortHandle,
    pending: Mutex<Option<QueryTask>>,
}

impl OperationTask {
    pub fn new(abort: AbortHandle, pending: QueryTask) -> Self {
        Self { abort, pending: Mutex::new(Some(pending)) }
    }

    /// Submit once to the Tokio runtime.
    pub fn start(&self) {
        let pending = self.pending.lock().unwrap_or_else(std::sync::PoisonError::into_inner).take();
        if let Some(task) = pending {
            pyo3_async_runtimes::tokio::get_runtime().spawn(task);
        }
    }
}

#[pymethods]
impl OperationTask {
    /// Dropping the native operation also activates the driver's SQL cancellation guard.
    fn __call__(&self, future: &Bound<'_, PyAny>) -> PyResult<()> {
        if future.call_method0(intern!(future.py(), "cancelled"))?.is_truthy()? {
            self.abort.abort();
        }
        Ok(())
    }
}
