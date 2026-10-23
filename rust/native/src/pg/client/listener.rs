use crate::asyncio::completion::future_into_py;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex as StdMutex};

use pyo3::prelude::*;

/// The handle `Client::listen()` returns. A plain `std::sync::Mutex`: no lock is held across an
/// `.await`. `weakref`, since the listening task holds it weakly.
#[pyclass(weakref)]
pub struct Listener {
    pub(super) inner: Arc<StdMutex<Option<tokio_postgres::Client>>>,
    /// Shared with `open_listener`'s background task - set there the moment the notification
    /// stream breaks on its own (not just on an explicit `.close()`), so `is_closed()` can report
    /// a dead connection honestly instead of only ever reacting to a caller's own `.close()`.
    pub(super) closed: Arc<AtomicBool>,
}

#[pymethods]
impl Listener {
    /// Closes the dedicated connection this listener holds and stops delivering notifications.
    /// Idempotent - closing an already-closed (or not-yet-connected) listener is a no-op.
    fn close<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        let closed = self.closed.clone();
        future_into_py(py, async move {
            // Dropping the only `tokio_postgres::Client` closes the connection; the listening task
            // then ends on its own.
            inner.lock().unwrap_or_else(std::sync::PoisonError::into_inner).take();
            closed.store(true, Ordering::Release);
            Ok(())
        })
    }

    fn is_closed(&self) -> bool {
        self.closed.load(Ordering::Acquire)
    }
}
