//! Limits belonging to PostgreSQL's value formats.

/// PostgreSQL MAXDIM, defined in src/include/utils/array.h.
pub(crate) const MAX_ARRAY_DIMENSIONS: usize = 6;

/// Bound recursive native frames and the resulting Value tree independently of a server's stack.
pub(crate) const MAX_DECODE_NESTING: usize = 256;
