//! `rust.native.clock` - the system's precise wall clock, for Pythons whose `datetime.now()` reads a
//! coarser one (Windows before Python 3.13).

use std::time::{SystemTime, UNIX_EPOCH};

use pyo3::exceptions::PyOSError;
use pyo3::prelude::*;
use pyo3::types::PyDateTime;

/// The current moment in UTC, aware - `datetime.timezone.utc` as its tzinfo.
#[pyfunction]
fn get_utc_now(py: Python<'_>) -> PyResult<Bound<'_, PyDateTime>> {
    Ok(chrono::Utc::now().into_pyobject(py)?.cast_into::<PyDateTime>()?)
}

/// The current POSIX time as whole seconds and the microseconds past them.
#[pyfunction]
fn get_posix_time() -> PyResult<(u64, u32)> {
    let elapsed = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|error| PyOSError::new_err(format!("the system clock is before 1970: {error}")))?;
    Ok((elapsed.as_secs(), elapsed.subsec_micros()))
}

pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(get_utc_now, module)?)?;
    module.add_function(wrap_pyfunction!(get_posix_time, module)?)?;
    Ok(())
}
