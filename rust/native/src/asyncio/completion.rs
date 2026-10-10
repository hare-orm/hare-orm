//! The bridge from a native driver's tokio futures to asyncio futures - `future_into_py()`, the one
//! every async method returns through. A finished query goes to its event loop's
//! `CompletionQueue`, which resolves the asyncio future on the loop's own thread.
//!
//! Cancelling the asyncio future aborts the tokio task, which drops the query's future where it
//! awaits - the driver cancels its running statement on that drop (`pg`'s `CancelQueryOnDrop`). A panic inside a future
//! resolves its asyncio future with a `RuntimeError` instead of leaving it pending forever.

use std::future::Future;
use std::panic::AssertUnwindSafe;

use futures_util::FutureExt;
use pyo3::exceptions::PyRuntimeError;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::IntoPyObjectExt;

use crate::asyncio::abort_on_cancel::AbortOnCancel;
use crate::asyncio::completion_queue::{Completion, CompletionQueue, Resolver};

/// `asyncio.get_running_loop`, resolved once.
static GET_RUNNING_LOOP: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

/// The text of a panic's payload.
fn get_panic_message(payload: &(dyn std::any::Any + Send)) -> String {
    if let Some(text) = payload.downcast_ref::<&str>() {
        (*text).to_string()
    } else if let Some(text) = payload.downcast_ref::<String>() {
        text.clone()
    } else {
        "unknown error".to_string()
    }
}

/// Runs `fut` on the tokio runtime and returns an asyncio future of the running event loop that
/// resolves with its result - a drop-in for `pyo3_async_runtimes::tokio::future_into_py`.
pub fn future_into_py<F, T>(py: Python<'_>, fut: F) -> PyResult<Bound<'_, PyAny>>
where
    F: Future<Output = PyResult<T>> + Send + 'static,
    T: for<'py> IntoPyObject<'py> + Send + 'static,
{
    let get_running_loop = GET_RUNNING_LOOP.get_or_try_init(py, || -> PyResult<Py<PyAny>> {
        Ok(py.import("asyncio")?.getattr("get_running_loop")?.unbind())
    })?;
    let event_loop = get_running_loop.bind(py).call0()?;
    let queue = CompletionQueue::get(py, &event_loop)?;
    let py_future = event_loop.call_method0(intern!(py, "create_future"))?;
    let future = py_future.clone().unbind();
    let completion_queue = queue.clone_ref(py);
    let (query, abort_handle) = futures_util::future::abortable(async move {
        let resolve: Resolver = match AssertUnwindSafe(fut).catch_unwind().await {
            Ok(result) => Box::new(move |py: Python<'_>| result.and_then(|value| value.into_py_any(py))),
            Err(payload) => {
                let message = get_panic_message(payload.as_ref());
                Box::new(move |_py: Python<'_>| {
                    Err(PyRuntimeError::new_err(format!("rust future panicked: {message}")))
                })
            }
        };
        CompletionQueue::enqueue(queue, Completion { future, resolve });
    });
    // An aborted query ends without a result - its asyncio future is cancelled already.
    CompletionQueue::spawn_with_turn(
        completion_queue.bind(py),
        &event_loop,
        Box::pin(async move {
            let _ = query.await;
        }),
    )?;
    py_future.call_method1(intern!(py, "add_done_callback"), (AbortOnCancel { task: abort_handle },))?;
    Ok(py_future)
}
