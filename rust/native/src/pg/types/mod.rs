//! Wire formats of the PostgreSQL types `Value` encodes and decodes, one file per type.

pub mod array;
pub mod bit_string;
pub mod composite;
pub(crate) mod constants;
pub mod datetime;
pub mod interval;
pub mod json;
pub mod mac_address;
pub mod money;
pub mod network;
pub mod numeric;
pub mod range;
pub mod text_search_query;
pub mod text_search_vector;
pub mod time_with_zone;
pub mod vector;
