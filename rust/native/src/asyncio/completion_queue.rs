//! `CompletionQueue` - the finished queries of one event loop, resolved on that loop in one callback.
//! A finished query is only pushed here, without the GIL; the loop is woken once, when the queue goes
//! from empty to non-empty, so a quiet connection resolves each query right away and a busy one a
//! whole batch per wake-up.
//!
//! The loop listens on a socket pair of the queue - `add_reader()`, or a pending `recv()` on a
//! proactor loop - and the tokio worker that finished a query wakes it with a byte, without the GIL
//! and without another thread. A loop that can do neither is woken through
//! `call_soon_threadsafe()` on the `LoopWaker` thread.
//!
//! The queue holds its loop weakly and the queues are found by the loop's address - an entry leaves
//! when its loop is collected.

use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{LazyLock, Mutex, OnceLock};

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyCFunction, PyTuple, PyWeakrefMethods, PyWeakrefReference};
use pyo3::{PyTraverseError, PyVisit};

use crate::asyncio::loop_waker::LoopWaker;
use crate::asyncio::wake_socket::WakeSocket;

/// Turns a finished query's Rust result into its Python value, on the event loop's thread.
pub type Resolver = Box<dyn FnOnce(Python<'_>) -> PyResult<Py<PyAny>> + Send>;

/// A query to run on the tokio runtime.
pub type QueryTask = Pin<Box<dyn Future<Output = ()> + Send>>;

/// One finished query waiting for its event loop: the asyncio future and how to resolve it.
pub struct Completion {
    pub future: Py<PyAny>,
    pub resolve: Resolver,
}

/// How a queue wakes its loop.
enum Waking {
    /// A byte written to `writer` wakes the loop, which listens on `reader` - emptied through
    /// `reader_copy` on a loop that only reports it readable.
    Socket { writer: WakeSocket, reader: Py<PyAny>, reader_copy: Option<WakeSocket> },
    /// `call_soon_threadsafe()` on the `LoopWaker` thread.
    LoopWaker,
}

/// The size of one proactor `recv()` of the wake socket.
const WAKE_RECEIVE_SIZE: usize = 4096;

/// Event loop address -> its queue.
static QUEUES_BY_LOOP: LazyLock<Mutex<HashMap<usize, Py<CompletionQueue>>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));
/// `socket.socketpair`, resolved once.
static SOCKETPAIR: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

#[pyclass(frozen)]
pub struct CompletionQueue {
    event_loop: Py<PyWeakrefReference>,
    /// Set once, on the loop's thread, before the first query of the loop is spawned.
    waking: OnceLock<Waking>,
    pending: Mutex<Vec<Completion>>,
    /// The queries started in the current turn of the loop, spawned together when the turn ends: the
    /// runtime's worker is woken once for them, not once per query.
    turn_tasks: Mutex<Vec<QueryTask>>,
    /// Whether a drain is already scheduled on the loop - set by the producer that finds the
    /// queue without one, cleared by the drain before it takes the queue, so a result pushed
    /// after that point schedules the next drain.
    drain_scheduled: AtomicBool,
}

impl CompletionQueue {
    fn lock_queues() -> std::sync::MutexGuard<'static, HashMap<usize, Py<CompletionQueue>>> {
        QUEUES_BY_LOOP.lock().unwrap_or_else(std::sync::PoisonError::into_inner)
    }

    /// The queue of `event_loop`, made on its first query.
    pub fn get(py: Python<'_>, event_loop: &Bound<'_, PyAny>) -> PyResult<Py<CompletionQueue>> {
        let address = event_loop.as_ptr() as usize;
        let found = Self::lock_queues().get(&address).map(|queue| queue.clone_ref(py));
        if let Some(queue) = found {
            if queue.get().event_loop.bind(py).upgrade().is_some_and(|alive| alive.is(event_loop)) {
                return Ok(queue);
            }
        }
        let queue = Bound::new(py, Self::new(event_loop, address)?)?;
        let waking = Self::listen(&queue, event_loop).unwrap_or(Waking::LoopWaker);
        let _ = queue.get().waking.set(waking);
        let replaced = Self::lock_queues().insert(address, queue.clone().unbind());
        // Dropped once the lock is released - a queue's finalization may run Python code.
        drop(replaced);
        Ok(queue.unbind())
    }

    /// A queue of `event_loop`, which leaves the registry when the loop is collected.
    fn new(event_loop: &Bound<'_, PyAny>, address: usize) -> PyResult<Self> {
        let py = event_loop.py();
        let forget = PyCFunction::new_closure(py, None, None, move |arguments: &Bound<'_, PyTuple>, _| {
            let weak_loop = arguments.get_item(0)?;
            let removed = {
                let mut queues = Self::lock_queues();
                let is_this_loop = queues
                    .get(&address)
                    .is_some_and(|queue| queue.get().event_loop.bind(arguments.py()).is(&weak_loop));
                if is_this_loop {
                    queues.remove(&address)
                } else {
                    None
                }
            };
            drop(removed);
            PyResult::Ok(())
        })?;
        Ok(CompletionQueue {
            event_loop: PyWeakrefReference::new_with(event_loop, forget)?.unbind(),
            waking: OnceLock::new(),
            pending: Mutex::new(Vec::new()),
            turn_tasks: Mutex::new(Vec::new()),
            drain_scheduled: AtomicBool::new(false),
        })
    }

    /// Makes `event_loop` listen on a socket pair of `queue` - an error leaves the loop to the
    /// `LoopWaker`.
    fn listen(queue: &Bound<'_, Self>, event_loop: &Bound<'_, PyAny>) -> PyResult<Waking> {
        let py = queue.py();
        let socketpair = SOCKETPAIR.get_or_try_init(py, || -> PyResult<Py<PyAny>> {
            Ok(py.import("socket")?.getattr("socketpair")?.unbind())
        })?;
        let pair = socketpair.bind(py).call0()?;
        let (reader, writer) = (pair.get_item(0)?, pair.get_item(1)?);
        let listened = (|| -> PyResult<Waking> {
            reader.call_method1(intern!(py, "setblocking"), (false,))?;
            let writer_copy = WakeSocket::duplicate(writer.call_method0(intern!(py, "fileno"))?.extract()?)?;
            let reader_descriptor: i64 = reader.call_method0(intern!(py, "fileno"))?.extract()?;
            if let Ok(proactor) = event_loop.getattr(intern!(py, "_proactor")) {
                Self::receive(queue, &proactor, &reader)?;
                return Ok(Waking::Socket { writer: writer_copy, reader: reader.clone().unbind(), reader_copy: None });
            }
            event_loop.call_method1(
                intern!(py, "add_reader"),
                (reader_descriptor, queue.getattr(intern!(py, "on_readable"))?),
            )?;
            Ok(Waking::Socket {
                writer: writer_copy,
                reader: reader.clone().unbind(),
                reader_copy: Some(WakeSocket::duplicate(reader_descriptor)?),
            })
        })();
        // The queue writes through a socket of its own; the reader stays open while the loop listens.
        writer.call_method0(intern!(py, "close"))?;
        if listened.is_err() {
            reader.call_method0(intern!(py, "close"))?;
        }
        listened
    }

    /// Waits for the next wake byte on a proactor loop.
    fn receive(queue: &Bound<'_, Self>, proactor: &Bound<'_, PyAny>, reader: &Bound<'_, PyAny>) -> PyResult<()> {
        let py = queue.py();
        let received = proactor.call_method1(intern!(py, "recv"), (reader, WAKE_RECEIVE_SIZE))?;
        received.call_method1(intern!(py, "add_done_callback"), (queue.getattr(intern!(py, "on_received"))?,))?;
        Ok(())
    }

    /// Keeps a query to be spawned with the others the current turn of the loop starts - the first
    /// one schedules the spawn at the end of the turn. Runs on the loop's thread.
    pub fn spawn_with_turn(queue: &Bound<'_, Self>, event_loop: &Bound<'_, PyAny>, task: QueryTask) -> PyResult<()> {
        let py = queue.py();
        let state = queue.get();
        let is_first = {
            let mut turn_tasks = state.turn_tasks.lock().unwrap_or_else(std::sync::PoisonError::into_inner);
            turn_tasks.push(task);
            turn_tasks.len() == 1
        };
        if is_first {
            // Bound anew for each turn: a bound method kept by the queue would hold the queue
            // itself, a cycle the collector can't break - the queue and its wake socket would
            // outlive the event loop.
            let callback = queue.getattr(intern!(py, "spawn_turn_tasks"))?;
            event_loop.call_method1(intern!(py, "call_soon"), (callback,))?;
        }
        Ok(())
    }

    /// Queues a finished query and wakes the loop when no drain is scheduled yet. Runs on a tokio
    /// worker, without the GIL.
    pub fn enqueue(queue: Py<CompletionQueue>, completion: Completion) {
        let state = queue.get();
        state.pending.lock().unwrap_or_else(std::sync::PoisonError::into_inner).push(completion);
        if state.drain_scheduled.swap(true, Ordering::SeqCst) {
            return;
        }
        match state.waking.get() {
            Some(Waking::Socket { writer, .. }) => writer.wake(),
            _ => LoopWaker::run_with_gil(Box::new(move |py| Self::schedule_drain(py, &queue))),
        }
    }

    /// Puts the drain on the loop through `call_soon_threadsafe()`.
    fn schedule_drain(py: Python<'_>, queue: &Py<CompletionQueue>) {
        let state = queue.get();
        let scheduled = match state.event_loop.bind(py).upgrade() {
            Some(event_loop) => event_loop.call_method1(intern!(py, "call_soon_threadsafe"), (queue,)).is_ok(),
            None => false,
        };
        if !scheduled {
            // The loop is closed or collected - nothing will await these futures any more.
            state.drain_scheduled.store(false, Ordering::SeqCst);
        }
    }

    /// Resolves every future queued since the last drain.
    fn drain(&self, py: Python<'_>) {
        self.drain_scheduled.store(false, Ordering::SeqCst);
        let completions = std::mem::take(&mut *self.pending.lock().unwrap_or_else(std::sync::PoisonError::into_inner));
        for completion in completions {
            if let Err(error) = Self::resolve(py, completion) {
                error.write_unraisable(py, None);
            }
        }
    }

    /// Resolves one future - nothing when it is already done (cancelled while its query ran).
    fn resolve(py: Python<'_>, completion: Completion) -> PyResult<()> {
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
}

#[pymethods]
impl CompletionQueue {
    /// The drain `call_soon_threadsafe()` schedules.
    fn __call__(&self, py: Python<'_>) {
        self.drain(py);
    }

    /// The `call_soon()` callback of a turn's first query: spawns the queries the turn started.
    fn spawn_turn_tasks(&self) {
        let tasks = std::mem::take(&mut *self.turn_tasks.lock().unwrap_or_else(std::sync::PoisonError::into_inner));
        let runtime = pyo3_async_runtimes::tokio::get_runtime();
        for task in tasks {
            runtime.spawn(task);
        }
    }

    /// The `add_reader()` callback: the wake socket is readable.
    fn on_readable(&self, py: Python<'_>) {
        if let Some(Waking::Socket { reader_copy: Some(reader_copy), .. }) = self.waking.get() {
            reader_copy.discard_bytes();
        }
        self.drain(py);
    }

    /// The done callback of a proactor `recv()` of the wake socket: drains, then waits for the next
    /// wake byte - unless the receive was cancelled or failed, as when the loop closes.
    fn on_received(slf: &Bound<'_, Self>, received: &Bound<'_, PyAny>) -> PyResult<()> {
        let py = slf.py();
        if received.call_method0(intern!(py, "cancelled"))?.is_truthy()?
            || !received.call_method0(intern!(py, "exception"))?.is_none()
        {
            return Ok(());
        }
        slf.get().drain(py);
        let Some(event_loop) = slf.get().event_loop.bind(py).upgrade() else {
            return Ok(());
        };
        if event_loop.call_method0(intern!(py, "is_closed"))?.is_truthy()? {
            return Ok(());
        }
        if let Some(Waking::Socket { reader, .. }) = slf.get().waking.get() {
            Self::receive(slf, &event_loop.getattr(intern!(py, "_proactor"))?, reader.bind(py))?;
        }
        Ok(())
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.event_loop)?;
        if let Some(Waking::Socket { reader, .. }) = self.waking.get() {
            visit.call(reader)?;
        }
        Ok(())
    }
}

impl Drop for CompletionQueue {
    fn drop(&mut self) {
        if let Some(Waking::Socket { reader, .. }) = self.waking.get() {
            Python::attach(|py| {
                let _ = reader.bind(py).call_method0(intern!(py, "close"));
            });
        }
    }
}
