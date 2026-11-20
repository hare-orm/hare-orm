//! Shared resource accounting for a recursively decoded PostgreSQL value.

use crate::pg::types::constants::MAX_DECODE_NESTING;
use crate::pg::value::{BoxError, Value};

pub(crate) struct PgDecodeContext {
    depth: usize,
    remaining_nodes: usize,
    result_bytes: usize,
}

impl PgDecodeContext {
    pub(crate) fn new(wire_bytes: usize) -> Self {
        // Lengths, counts and dimensions consume wire bytes too. Two slots per byte
        // cover array nesting, nullable slots and separately allocated range bounds.
        Self { depth: 0, remaining_nodes: wire_bytes.saturating_mul(2).saturating_add(1), result_bytes: 0 }
    }

    pub(crate) fn reserve_values(&mut self, count: usize) -> Result<(), BoxError> {
        self.remaining_nodes =
            self.remaining_nodes.checked_sub(count).ok_or("decoded value count exceeds its wire representation")?;
        self.reserve_bytes(count.checked_mul(std::mem::size_of::<Value>()).ok_or("decoded value size overflow")?)
    }

    pub(crate) fn reserve_bytes(&mut self, count: usize) -> Result<(), BoxError> {
        self.result_bytes = self
            .result_bytes
            .checked_add(count)
            .filter(|size| isize::try_from(*size).is_ok())
            .ok_or("decoded result size exceeds addressable memory")?;
        Ok(())
    }

    pub(crate) fn visit<T>(
        &mut self,
        levels: usize,
        operation: impl FnOnce(&mut Self) -> Result<T, BoxError>,
    ) -> Result<T, BoxError> {
        let depth = self.depth.checked_add(levels).ok_or("decoded nesting depth overflow")?;
        if depth > MAX_DECODE_NESTING {
            return Err("PostgreSQL value exceeds native decoding nesting depth".into());
        }
        let previous_depth = self.depth;
        self.depth = depth;
        let result = operation(self);
        self.depth = previous_depth;
        result
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn depth_is_restored_after_an_error_and_slots_are_bounded_by_input() {
        let mut context = PgDecodeContext::new(8);
        let result: Result<(), BoxError> = context.visit(3, |_| Err("failure".into()));
        assert!(result.is_err());
        assert_eq!(context.depth, 0);
        assert!(context.visit(MAX_DECODE_NESTING + 1, |_| Ok(())).is_err());
        assert_eq!(context.depth, 0);
        assert!(context.reserve_values(18).is_err());
        assert!(context.reserve_bytes(usize::MAX).is_err());
    }
}
