//! An `IntEnumField`/`CharEnumField`: read through the enum's value-to-member map, a member of the
//! field's own enum written as its stored value.

mod enumeration_read;
mod enumeration_write;

pub use enumeration_read::EnumerationRead;
pub use enumeration_write::EnumerationWrite;
