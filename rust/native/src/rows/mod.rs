//! `rust.native.rows` - reading driver rows into model instances and writing instances into rows of
//! bound values, through the field codecs.

pub mod attribute_capture;
pub mod attribute_setting;
pub mod combined_reader;
pub mod json_check;
pub mod json_text;
pub mod model_constructor;
pub mod model_reader;
pub mod model_writer;
pub mod moment_ticks;
pub mod prefetched_rows;
pub mod result_row;
pub mod result_rows;
pub mod value_types;
pub mod values_reader;

use pyo3::prelude::*;

use crate::codecs::field_codec::FieldCodec;

/// Fills the `rows` submodule.
pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<FieldCodec>()?;
    module.add_class::<crate::codecs::value_checks::ValueChecks>()?;
    module.add_class::<model_constructor::ModelConstructor>()?;
    module.add_class::<model_reader::ModelReader>()?;
    module.add_class::<model_writer::ModelWriter>()?;
    module.add_class::<values_reader::ValuesReader>()?;
    module.add_function(wrap_pyfunction!(attribute_capture::capture_attribute_values, module)?)?;
    module.add_function(wrap_pyfunction!(attribute_setting::set_attribute_values, module)?)?;
    module.add_function(wrap_pyfunction!(moment_ticks::get_moment_ticks, module)?)?;
    module.add_function(wrap_pyfunction!(combined_reader::read_combined_rows, module)?)?;
    module.add_function(wrap_pyfunction!(json_check::check_json_storable, module)?)?;
    module.add_function(wrap_pyfunction!(json_text::encode_json, module)?)?;
    module.add_function(wrap_pyfunction!(value_types::are_all_of_types, module)?)?;
    module.add_function(wrap_pyfunction!(prefetched_rows::group_by_attribute, module)?)?;
    module.add_function(wrap_pyfunction!(prefetched_rows::group_by_item, module)?)?;
    module.add_function(wrap_pyfunction!(prefetched_rows::group_related, module)?)?;
    module.add_function(wrap_pyfunction!(prefetched_rows::group_rows_by_owner, module)?)?;
    module.add_function(wrap_pyfunction!(prefetched_rows::set_prefetched_rows, module)?)?;
    module.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
