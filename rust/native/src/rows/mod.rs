//! Independent row, attribute, JSON and wire-format algorithms.

pub mod attribute_capture;
pub mod attribute_setting;
pub mod json_check;
pub mod moment_ticks;
pub mod postgres_moments;
pub mod postgres_value_text;
pub mod prefetched_rows;
pub mod value_types;

use pyo3::prelude::*;

/// Fills the `rows` submodule.
pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(attribute_capture::capture_attribute_values, module)?)?;
    module.add_function(wrap_pyfunction!(attribute_setting::set_attribute_values, module)?)?;
    module.add_function(wrap_pyfunction!(attribute_setting::set_missing_attribute_values, module)?)?;
    module.add_function(wrap_pyfunction!(attribute_setting::stamp_attribute_values, module)?)?;
    module.add_function(wrap_pyfunction!(moment_ticks::get_moment_ticks, module)?)?;
    module.add_function(wrap_pyfunction!(json_check::check_json_storable, module)?)?;
    module.add_function(wrap_pyfunction!(postgres_moments::read_postgres_date, module)?)?;
    module.add_function(wrap_pyfunction!(postgres_moments::write_postgres_date, module)?)?;
    module.add_function(wrap_pyfunction!(postgres_moments::read_postgres_timestamptz, module)?)?;
    module.add_function(wrap_pyfunction!(postgres_moments::write_postgres_timestamptz, module)?)?;
    module.add_function(wrap_pyfunction!(postgres_value_text::read_tsvector_text, module)?)?;
    module.add_function(wrap_pyfunction!(postgres_value_text::read_hstore_text, module)?)?;
    module.add_function(wrap_pyfunction!(value_types::are_all_of_types, module)?)?;
    module.add_function(wrap_pyfunction!(prefetched_rows::group_by_item, module)?)?;
    module.add_function(wrap_pyfunction!(prefetched_rows::group_related, module)?)?;
    module.add_function(wrap_pyfunction!(prefetched_rows::group_rows_by_owner, module)?)?;
    module.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
