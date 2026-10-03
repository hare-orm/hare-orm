//! Field codecs: how a value read from a driver becomes the value a model attribute holds, and how
//! an attribute value becomes the value bound for its column. A codec handles the common shapes of
//! its type itself; any other value goes through the field's own Python method, so the result is
//! always what that method gives.

pub mod array;
pub mod binary;
pub mod boolean;
pub mod date;
pub mod datetime;
pub mod decimal;
pub mod enumeration;
pub mod field_codec;
pub mod inline_checks;
pub mod iso_text;
pub mod json;
pub mod options;
pub mod range;
pub mod read_codec;
pub mod scalar;
pub mod time;
pub mod timedelta;
pub mod uuid;
pub mod value_checks;
pub mod write_codec;
