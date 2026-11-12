//! Native futures finish on their originating asyncio loop, including conversion errors and panics.

use std::future::Future;
use std::panic::AssertUnwindSafe;

use futures_util::FutureExt;
use pyo3::exceptions::PyRuntimeError;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::PyWeakrefReference;
use pyo3::IntoPyObjectExt;

use crate::asyncio::completion_queue::{Completion, CompletionQueue, Conversion};
use crate::asyncio::operation_task::OperationTask;

/// Runs a native operation for the running asyncio loop. The operation runs on Tokio without the
/// caller's `contextvars`: no native code calls back into Python with them, and done-callbacks run
/// in the context asyncio captured when they were added. Cancellation drops the operation; its
/// driver retains responsibility for cancelling SQL and returning the connection safely.
pub fn future_into_py<F, T>(py: Python<'_>, future: F) -> PyResult<Bound<'_, PyAny>>
where
    F: Future<Output = PyResult<T>> + Send + 'static,
    T: for<'py> IntoPyObject<'py> + Send + 'static,
{
    let event_loop = pyo3_async_runtimes::get_running_loop(py)?;
    let queue = CompletionQueue::get(py, &event_loop)?;
    let python_future = event_loop.call_method0(intern!(py, "create_future"))?;
    let weak_future = PyWeakrefReference::new(&python_future)?.unbind();
    let completion_queue = queue.clone_ref(py);
    let (operation, abort_handle) = futures_util::future::abortable(async move {
        let conversion: Conversion = match AssertUnwindSafe(future).catch_unwind().await {
            Ok(result) => Box::new(move |py| result.and_then(|value| value.into_py_any(py))),
            Err(payload) => {
                let message = payload.downcast_ref::<&str>().map_or_else(
                    || payload.downcast_ref::<String>().cloned().unwrap_or_else(|| "unknown error".to_owned()),
                    |message| (*message).to_owned(),
                );
                Box::new(move |_| Err(PyRuntimeError::new_err(format!("rust future panicked: {message}"))))
            }
        };
        CompletionQueue::enqueue(queue, Completion { future: weak_future, conversion });
    });
    let task = Bound::new(
        py,
        OperationTask::new(
            abort_handle,
            Box::pin(async move {
                let _ = operation.await;
            }),
        ),
    )?;
    // The future keeps its task alive; the start queue refers to the task weakly, so a closed loop
    // releases both.
    python_future.call_method1(intern!(py, "add_done_callback"), (&task,))?;
    CompletionQueue::spawn_with_turn(completion_queue.bind(py), &event_loop, &task)?;
    Ok(python_future)
}
