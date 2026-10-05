//! `LoopWaker` - the one thread that schedules `CompletionQueue` drains through
//! `call_soon_threadsafe()`, for an event loop that can't listen on a socket of the queue. It keeps
//! its Python thread state between jobs - a pool thread would make a new one for each - and holds
//! the GIL only while a job runs.

use std::sync::mpsc::{channel, Receiver, Sender};
use std::sync::OnceLock;

use pyo3::prelude::*;

/// Work that needs the GIL.
pub type GilJob = Box<dyn FnOnce(Python<'_>) + Send>;

pub struct LoopWaker;

impl LoopWaker {
    /// Where jobs are sent - the thread starts with the first one.
    fn get_sender() -> &'static Sender<GilJob> {
        static SENDER: OnceLock<Sender<GilJob>> = OnceLock::new();
        SENDER.get_or_init(|| {
            let (sender, receiver) = channel::<GilJob>();
            std::thread::Builder::new()
                .name("hare-loop-waker".to_string())
                .spawn(move || LoopWaker::run(receiver))
                .expect("the loop waker thread starts");
            sender
        })
    }

    /// Runs `job` with the GIL on the waker thread, without waiting for it.
    pub fn run_with_gil(job: GilJob) {
        // The thread only ends with the process - a send never fails while it runs.
        let _ = LoopWaker::get_sender().send(job);
    }

    /// Waits for jobs with the GIL released, then runs every job already sent at once.
    fn run(receiver: Receiver<GilJob>) {
        Python::attach(|py| {
            let mut receiver = receiver;
            loop {
                let (job, returned_receiver) = py.detach(move || {
                    let job = receiver.recv();
                    (job, receiver)
                });
                receiver = returned_receiver;
                let Ok(job) = job else {
                    return;
                };
                job(py);
                while let Ok(job) = receiver.try_recv() {
                    job(py);
                }
            }
        });
    }
}
