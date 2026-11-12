//! Independent row, attribute, JSON and wire-format algorithms.

pub mod attribute_capture;
pub mod attribute_setting;
pub mod column_reader;
pub mod field_validation;
pub mod grouped_model_layout;
pub mod instance_constructor;
pub mod instance_serialization;
pub mod json_check;
pub mod json_column_reader;
pub mod json_reading;
pub mod model_result_layout;
pub mod model_row;
pub mod moment_ticks;
pub mod parameter_bindings;
pub mod parameter_slot;
pub mod postgres_moments;
pub mod postgres_value_text;
pub mod prefetched_rows;
pub mod typed_column_reader;
pub mod typed_column_writer;
pub mod value_check;
pub mod value_layout;
pub mod value_rows;
pub mod value_types;
pub mod value_writing;

use pyo3::prelude::*;

/// Fills the `rows` submodule.
pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<model_result_layout::ModelResultLayout>()?;
    module.add_function(wrap_pyfunction!(field_validation::validate_field, module)?)?;
    module.add_class::<grouped_model_layout::GroupedModelLayout>()?;
    module.add_function(wrap_pyfunction!(instance_serialization::serialize_instances, module)?)?;
    module.add_function(wrap_pyfunction!(prefetched_rows::group_by_attribute, module)?)?;
    module.add_function(wrap_pyfunction!(attribute_capture::capture_attribute_values, module)?)?;
    module.add_function(wrap_pyfunction!(attribute_capture::capture_persistence_snapshots, module)?)?;
    module.add_function(wrap_pyfunction!(attribute_setting::set_attribute_values, module)?)?;
    module.add_function(wrap_pyfunction!(attribute_setting::set_missing_attribute_values, module)?)?;
    module.add_function(wrap_pyfunction!(attribute_setting::stamp_attribute_values, module)?)?;
    module.add_function(wrap_pyfunction!(moment_ticks::get_moment_ticks, module)?)?;
    module.add_function(wrap_pyfunction!(json_check::check_json_storable, module)?)?;
    module.add_function(wrap_pyfunction!(json_reading::loads_json, module)?)?;
    module.add_class::<json_column_reader::JsonColumnReader>()?;
    module.add_class::<instance_constructor::InstanceConstructor>()?;
    module.add_class::<typed_column_reader::TypedColumnReader>()?;
    module.add_class::<value_check::ValueCheck>()?;
    module.add_class::<parameter_slot::ParameterSlot>()?;
    module.add_class::<parameter_bindings::ParameterBindings>()?;
    module.add_class::<typed_column_writer::TypedColumnWriter>()?;
    module.add_function(wrap_pyfunction!(value_writing::write_uuid_text, module)?)?;
    module.add_function(wrap_pyfunction!(value_writing::get_timedelta_microseconds, module)?)?;
    module.add_function(wrap_pyfunction!(value_writing::dumps_json, module)?)?;
    module.add_class::<model_row::ModelRowLayout>()?;
    module.add_class::<value_layout::ValueLayout>()?;
    module.add_function(wrap_pyfunction!(attribute_setting::copy_slot_values, module)?)?;
    module.add_function(wrap_pyfunction!(postgres_moments::read_postgres_date, module)?)?;
    module.add_function(wrap_pyfunction!(postgres_moments::write_postgres_date, module)?)?;
    module.add_function(wrap_pyfunction!(postgres_moments::read_postgres_timestamptz, module)?)?;
    module.add_function(wrap_pyfunction!(postgres_moments::write_postgres_timestamptz, module)?)?;
    module.add_function(wrap_pyfunction!(postgres_value_text::read_tsvector_text, module)?)?;
    module.add_function(wrap_pyfunction!(postgres_value_text::read_hstore_text, module)?)?;
    module.add_function(wrap_pyfunction!(value_types::are_all_of_types, module)?)?;
    module.add_function(wrap_pyfunction!(value_layout::read_row_values, module)?)?;
    module.add_function(wrap_pyfunction!(value_rows::read_values_row, module)?)?;
    module.add_function(wrap_pyfunction!(value_rows::read_values_batch, module)?)?;
    module.add_function(wrap_pyfunction!(prefetched_rows::group_by_item, module)?)?;
    module.add_function(wrap_pyfunction!(prefetched_rows::group_related, module)?)?;
    module.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
