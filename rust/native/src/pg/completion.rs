//! The bridge from this driver's tokio futures to asyncio futures - `future_into_py()`, the one
//! every async method returns through.
//!
//! `pyo3_async_runtimes::tokio::future_into_py` resolves each asyncio future on its own: per
//! query it spawns two tokio tasks and a blocking task, takes the GIL there and calls
//! `loop.call_soon_threadsafe()`, which writes to the event loop's self-pipe to wake it. Under
//! concurrent load those wake-ups and GIL hand-offs cost as much as the queries' own Python
//! work. Here a finished query's result is only pushed onto its event loop's `CompletionQueue`,
//! without the GIL; the loop is woken once, when the queue goes from empty to non-empty, and one
//! callback on the loop's own thread turns every queued result into a Python object and
//! resolves its future. A quiet connection still resolves each query right away - the queue is
//! empty every time - while a busy one resolves a whole batch per wake-up.
//!
//! Cancelling the asyncio future aborts the tokio task, which drops the query's future where it
//! awaits - `CancelQueryOnDrop` then cancels the running statement, as with the upstream bridge.
//! A panic inside a future resolves its asyncio future with a `RuntimeError` instead of leaving
//! it pending forever.

use std::future::Future;
use std::panic::AssertUnwindSafe;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;

use futures_util::FutureExt;
use pyo3::exceptions::PyRuntimeError;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::IntoPyObjectExt;
use tokio::task::AbortHandle;

/// Turns a finished query's Rust result into its Python value, on the event loop's thread.
type Resolver = Box<dyn FnOnce(Python<'_>) -> PyResult<Py<PyAny>> + Send>;

/// One finished query waiting for its event loop: the asyncio future and how to resolve it.
struct Completion {
    future: Py<PyAny>,
    resolve: Resolver,
}

/// The finished queries of one event loop - and, as a callable, the callback that resolves them
/// on that loop (`loop.call_soon_threadsafe(queue)`).
#[pyclass(frozen)]
struct CompletionQueue {
    event_loop: Py<PyAny>,
    pending: Mutex<Vec<Completion>>,
    /// Whether a drain is already scheduled on the loop - set by the producer that finds the
    /// queue without one, cleared by the drain before it takes the queue, so a result pushed
    /// after that point schedules the next drain.
    drain_scheduled: AtomicBool,
}

#[pymethods]
impl CompletionQueue {
    /// Resolves every future queued since the last drain.
    fn __call__(slf: &Bound<'_, Self>) {
        let py = slf.py();
        let queue = slf.get();
        queue.drain_scheduled.store(false, Ordering::SeqCst);
        let completions = std::mem::take(&mut *queue.pending.lock().unwrap_or_else(std::sync::PoisonError::into_inner));
        for completion in completions {
            if let Err(error) = resolve_completion(py, completion) {
                error.write_unraisable(py, None);
            }
        }
    }
}

/// Resolves one future - nothing when it is already done (cancelled while its query ran).
fn resolve_completion(py: Python<'_>, completion: Completion) -> PyResult<()> {
    let future = completion.future.bind(py);
    if future.call_method0(intern!(py, "done"))?.is_truthy()? {
        return Ok(());
    }
    match (completion.resolve)(py) {
        Ok(value) => future.call_method1(intern!(py, "set_result"), (value,))?,
        Err(error) => future.call_method1(intern!(py, "set_exception"), (error.value(py),))?,
    };
    Ok(())
}

/// The `add_done_callback` of an asyncio future: aborts its query's tokio task once the future
/// is cancelled.
#[pyclass(frozen)]
struct AbortOnCancel {
    task: AbortHandle,
}

#[pymethods]
impl AbortOnCancel {
    fn __call__(&self, future: &Bound<'_, PyAny>) -> PyResult<()> {
        if future.call_method0(intern!(future.py(), "cancelled"))?.is_truthy()? {
            self.task.abort();
        }
        Ok(())
    }
}

/// `asyncio.get_running_loop`, resolved once.
static GET_RUNNING_LOOP: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
/// Event loop -> its `CompletionQueue`, weakly keyed, so a closed loop takes its queue with it.
static QUEUES_BY_LOOP: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

/// The `CompletionQueue` of an event loop, made on its first query.
fn get_completion_queue(py: Python<'_>, event_loop: &Bound<'_, PyAny>) -> PyResult<Py<CompletionQueue>> {
    let queues = QUEUES_BY_LOOP.get_or_try_init(py, || -> PyResult<Py<PyAny>> {
        Ok(py.import("weakref")?.getattr("WeakKeyDictionary")?.call0()?.unbind())
    })?;
    let queues = queues.bind(py);
    if let Ok(queue) = queues.call_method1(intern!(py, "get"), (event_loop,))?.cast::<CompletionQueue>() {
        return Ok(queue.clone().unbind());
    }
    let queue = Py::new(
        py,
        CompletionQueue {
            event_loop: event_loop.clone().unbind(),
            pending: Mutex::new(Vec::new()),
            drain_scheduled: AtomicBool::new(false),
        },
    )?;
    queues.set_item(event_loop, queue.clone_ref(py))?;
    Ok(queue)
}

/// Queues a finished query and wakes its event loop when no drain is scheduled yet. Runs on a
/// tokio worker: the GIL is taken only to schedule the drain, on a blocking thread, so a worker
/// never waits for it.
fn enqueue(queue: Py<CompletionQueue>, completion: Completion) {
    let queue_state = queue.get();
    queue_state.pending.lock().unwrap_or_else(std::sync::PoisonError::into_inner).push(completion);
    if queue_state.drain_scheduled.swap(true, Ordering::SeqCst) {
        return;
    }
    tokio::task::spawn_blocking(move || {
        Python::attach(|py| {
            let queue = queue.bind(py);
            let scheduled = queue.get().event_loop.bind(py).call_method1(intern!(py, "call_soon_threadsafe"), (queue,));
            if scheduled.is_err() {
                // The loop is closed - nothing will await these futures any more.
                queue.get().drain_scheduled.store(false, Ordering::SeqCst);
            }
        });
    });
}

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
    let queue = get_completion_queue(py, &event_loop)?;
    let py_future = event_loop.call_method0(intern!(py, "create_future"))?;
    let future = py_future.clone().unbind();
    let task = pyo3_async_runtimes::tokio::get_runtime().spawn(async move {
        let resolve: Resolver = match AssertUnwindSafe(fut).catch_unwind().await {
            Ok(result) => Box::new(move |py: Python<'_>| result.and_then(|value| value.into_py_any(py))),
            Err(payload) => {
                let message = get_panic_message(payload.as_ref());
                Box::new(move |_py: Python<'_>| {
                    Err(PyRuntimeError::new_err(format!("rust future panicked: {message}")))
                })
            }
        };
        enqueue(queue, Completion { future, resolve });
    });
    py_future.call_method1(intern!(py, "add_done_callback"), (AbortOnCancel { task: task.abort_handle() },))?;
    Ok(py_future)
}
