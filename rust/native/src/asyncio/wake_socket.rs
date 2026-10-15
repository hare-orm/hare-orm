//! `WakeSocket` - a socket of its own on one end of the socket pair a `CompletionQueue` wakes its
//! event loop through: written to from a tokio worker without the GIL, or emptied on the loop.

use std::io::{Read, Write};

use crate::python::ffi;

pub struct WakeSocket {
    #[cfg(windows)]
    socket: std::net::TcpStream,
    #[cfg(unix)]
    socket: std::os::unix::net::UnixStream,
}

impl WakeSocket {
    /// A wake socket on the socket whose descriptor Python reports (`socket.fileno()`).
    pub fn duplicate(descriptor: i64) -> std::io::Result<Self> {
        Ok(WakeSocket { socket: ffi::duplicate_socket(descriptor)? })
    }

    /// Writes the byte that wakes the loop. A full pipe already holds a wake-up - as in asyncio's
    /// own `_write_to_self()`, that is no error.
    pub fn wake(&self) {
        let _ = (&self.socket).write(&[0]);
    }

    /// Reads every byte written so far.
    pub fn discard_bytes(&self) {
        let mut buffer = [0u8; 4096];
        while matches!((&self.socket).read(&mut buffer), Ok(read) if read > 0) {}
    }
}
