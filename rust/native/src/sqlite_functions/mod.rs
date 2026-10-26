//! `rust.native.sqlite_functions` - the functions hare registers on a SQLite
//! connection whose Python versions ran per row: date part extraction and truncation,
//! `GREATEST`/`LEAST`, date arithmetic, math, text, variance and standard deviation, JSON paths
//! and dates, day starts and the local now.

pub mod date_functions;
pub mod date_timestamp;
pub mod greatest_least;
pub mod iso_moment;
pub mod json_array_text;
pub mod json_datetime;
pub mod json_path;
pub mod json_string;
pub mod local_now;
pub mod math_functions;
pub mod statistic;
pub mod temporal_arithmetic;
pub mod text_functions;
pub mod zone_conversions;

use pyo3::prelude::*;

/// Fills the `sqlite_functions` submodule.
pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(json_array_text::get_json_array_text, module)?)?;
    module.add_class::<date_functions::DateFunctions>()?;
    module.add_class::<date_timestamp::DateTimestamp>()?;
    module.add_class::<greatest_least::GreatestLeast>()?;
    module.add_class::<json_datetime::JsonDatetime>()?;
    module.add_class::<json_path::JsonPath>()?;
    module.add_class::<local_now::LocalNow>()?;
    module.add_class::<math_functions::MathFunctions>()?;
    module.add_class::<statistic::Statistic>()?;
    module.add_class::<temporal_arithmetic::TemporalArithmetic>()?;
    module.add_class::<text_functions::TextFunctions>()?;
    Ok(())
}
