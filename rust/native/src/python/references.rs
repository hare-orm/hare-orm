//! Telling the garbage collector which Python objects a Rust value holds - a reference it isn't
//! told about keeps every cycle through it alive (a model, its cached readers, their fields' methods).

use pyo3::{PyTraverseError, PyVisit};

/// A value holding Python objects.
pub trait PythonReferences {
    /// Visits every Python object the value holds.
    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError>;
}
