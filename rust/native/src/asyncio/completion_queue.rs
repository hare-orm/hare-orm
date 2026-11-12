//! One owned Unix socket pair per loop batches wakeups without entering Python on Tokio workers.
//! The registry and queued completions refer to the loop and asyncio futures weakly. Closing and
//! collecting a loop therefore releases its queue, sockets and undelivered results.

use std::collections::HashMap;
use std::io::{Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::net::UnixStream;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{LazyLock, Mutex};

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyCFunction, PyTuple, PyWeakrefMethods, PyWeakrefReference};
use pyo3::{PyTraverseError, PyVisit};

use crate::asyncio::operation_task::OperationTask;

pub type Conversion = Box<dyn FnOnce(Python<'_>) -> PyResult<Py<PyAny>> + Send>;

pub struct Completion {
    pub future: Py<PyWeakrefReference>,
    pub conversion: Conversion,
}

static QUEUES_BY_LOOP: LazyLock<Mutex<HashMap<usize, Py<CompletionQueue>>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));

#[pyclass(frozen)]
pub struct CompletionQueue {
    event_loop: Py<PyWeakrefReference>,
    reader: UnixStream,
    writer: UnixStream,
    pending: Mutex<Vec<Completion>>,
    turn_tasks: Mutex<Vec<Py<PyWeakrefReference>>>,
    drain_scheduled: AtomicBool,
}

impl CompletionQueue {
    fn lock_queues() -> std::sync::MutexGuard<'static, HashMap<usize, Py<CompletionQueue>>> {
        QUEUES_BY_LOOP.lock().unwrap_or_else(std::sync::PoisonError::into_inner)
    }

    pub fn get(py: Python<'_>, event_loop: &Bound<'_, PyAny>) -> PyResult<Py<Self>> {
        let address = event_loop.as_ptr() as usize;
        let found = Self::lock_queues().get(&address).map(|queue| queue.clone_ref(py));
        if let Some(queue) = found {
            if queue.get().event_loop.bind(py).upgrade().is_some_and(|alive| alive.is(event_loop)) {
                return Ok(queue);
            }
        }
        let forget = PyCFunction::new_closure(py, None, None, move |arguments: &Bound<'_, PyTuple>, _| {
            let weak_loop = arguments.get_item(0)?;
            let removed = {
                let mut queues = Self::lock_queues();
                if queues.get(&address).is_some_and(|queue| queue.get().event_loop.bind(arguments.py()).is(&weak_loop))
                {
                    queues.remove(&address)
                } else {
                    None
                }
            };
            // Finalization may execute Python; never drop a queue while holding the registry lock.
            drop(removed);
            PyResult::Ok(())
        })?;
        let (reader, writer) = UnixStream::pair()?;
        reader.set_nonblocking(true)?;
        writer.set_nonblocking(true)?;
        let queue = Bound::new(
            py,
            Self {
                event_loop: PyWeakrefReference::new_with(event_loop, forget)?.unbind(),
                reader,
                writer,
                pending: Mutex::new(Vec::new()),
                turn_tasks: Mutex::new(Vec::new()),
                drain_scheduled: AtomicBool::new(false),
            },
        )?;
        event_loop.call_method1(
            intern!(py, "add_reader"),
            (queue.get().reader.as_raw_fd(), queue.getattr(intern!(py, "on_readable"))?),
        )?;
        let replaced = Self::lock_queues().insert(address, queue.clone().unbind());
        drop(replaced);
        Ok(queue.unbind())
    }

    /// Defer starts until the end of this loop turn, so Tokio wakes once for concurrent calls.
    pub fn spawn_with_turn(
        queue: &Bound<'_, Self>,
        event_loop: &Bound<'_, PyAny>,
        task: &Bound<'_, OperationTask>,
    ) -> PyResult<()> {
        let first = {
            let mut tasks = queue.get().turn_tasks.lock().unwrap_or_else(std::sync::PoisonError::into_inner);
            tasks.push(PyWeakrefReference::new(task)?.unbind());
            tasks.len() == 1
        };
        if first {
            event_loop.call_method1(
                intern!(queue.py(), "call_soon"),
                (queue.getattr(intern!(queue.py(), "spawn_turn_tasks"))?,),
            )?;
        }
        Ok(())
    }

    pub fn enqueue(queue: Py<Self>, completion: Completion) {
        let state = queue.get();
        state.pending.lock().unwrap_or_else(std::sync::PoisonError::into_inner).push(completion);
        if !state.drain_scheduled.swap(true, Ordering::SeqCst) {
            let mut writer = &state.writer;
            loop {
                match writer.write(&[1]) {
                    Err(error) if error.kind() == std::io::ErrorKind::Interrupted => (),
                    // WouldBlock means a previous wake byte is still readable.
                    _ => break,
                }
            }
        }
    }

    fn deliver(py: Python<'_>, completion: Completion) -> PyResult<()> {
        let Some(future) = completion.future.bind(py).upgrade() else {
            return Ok(());
        };
        if !future.call_method0(intern!(py, "done"))?.is_truthy()? {
            match (completion.conversion)(py) {
                Ok(value) => future.call_method1(intern!(py, "set_result"), (value,))?,
                Err(error) => future.call_method1(intern!(py, "set_exception"), (error.value(py),))?,
            };
        }
        Ok(())
    }
}

#[pymethods]
impl CompletionQueue {
    fn spawn_turn_tasks(&self, py: Python<'_>) {
        let tasks = std::mem::take(&mut *self.turn_tasks.lock().unwrap_or_else(std::sync::PoisonError::into_inner));
        if self.event_loop.bind(py).upgrade().is_none() {
            return;
        }
        for reference in tasks {
            if let Some(task) = reference.bind(py).upgrade() {
                if let Ok(task) = task.cast::<OperationTask>() {
                    task.get().start();
                }
            }
        }
    }

    fn on_readable(&self, py: Python<'_>) {
        let mut reader = &self.reader;
        let mut buffer = [0; 4096];
        loop {
            match reader.read(&mut buffer) {
                Ok(0) => break,
                Ok(_) => (),
                Err(error) if error.kind() == std::io::ErrorKind::Interrupted => (),
                Err(_) => break,
            }
        }
        self.drain_scheduled.store(false, Ordering::SeqCst);
        let completions = std::mem::take(&mut *self.pending.lock().unwrap_or_else(std::sync::PoisonError::into_inner));
        for completion in completions {
            if let Err(error) = Self::deliver(py, completion) {
                error.write_unraisable(py, None);
            }
        }
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.event_loop)
    }
}
