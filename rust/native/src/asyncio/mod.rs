//! The bridge every native driver's tokio futures take to asyncio: `future_into_py()`, the
//! `CompletionQueue` of each event loop and the ways it wakes the loop.

pub mod abort_on_cancel;
pub mod completion;
pub mod completion_queue;
pub mod loop_waker;
pub mod wake_socket;
