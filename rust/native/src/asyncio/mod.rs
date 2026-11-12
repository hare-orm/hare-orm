//! Batched delivery between the native runtime and a Linux asyncio event loop.

mod completion;
mod completion_queue;
mod operation_task;

pub use completion::future_into_py;
