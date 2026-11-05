//! Bounded LRU metadata attached to one physical connection.
//! No pool-level identity map survives a connection's closure.

use lru::LruCache;
use std::num::NonZeroUsize;
use tokio_postgres::types::Type;

/// Recency metadata lives and dies with the physical connection's statement cache.
pub(crate) struct BoundedStatementTracker {
    entries: Option<LruCache<(String, Vec<Type>), ()>>,
}

impl BoundedStatementTracker {
    pub(crate) fn new(limit: usize) -> Self {
        // Grow with actual statements instead of reserving the configured maximum upfront.
        Self { entries: NonZeroUsize::new(limit).map(LruCache::sparse) }
    }

    pub(crate) fn clear(&mut self) {
        if let Some(entries) = &mut self.entries {
            entries.clear();
        }
    }

    /// Record a completed preparation and evict at most one least-recently-used statement.
    pub(crate) fn touch(&mut self, sql: &str, types: &[Type]) -> Option<(String, Vec<Type>)> {
        let key = (sql.to_owned(), types.to_owned());
        let Some(entries) = &mut self.entries else {
            return Some(key);
        };
        // A hit only refreshes recency: push would report the replaced entry as well as evictions.
        if entries.get(&key).is_some() {
            return None;
        }
        entries.push(key, ()).map(|(key, ())| key)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bounds_statements_and_refreshes_recency() {
        let mut tracker = BoundedStatementTracker::new(2);
        assert_eq!(tracker.touch("select 1", &[]), None);
        assert_eq!(tracker.touch("select 2", &[]), None);
        assert_eq!(tracker.touch("select 1", &[]), None);
        assert_eq!(tracker.touch("select 3", &[]), Some(("select 2".into(), vec![])));
        assert_eq!(tracker.touch("select 4", &[]), Some(("select 1".into(), vec![])));
    }

    #[test]
    fn parameter_types_are_part_of_the_statement_identity() {
        let mut tracker = BoundedStatementTracker::new(2);
        tracker.touch("select $1", &[Type::INT4]);
        tracker.touch("select $1", &[Type::TEXT]);
        tracker.touch("select $1", &[Type::INT4]);
        assert_eq!(tracker.touch("select $1", &[Type::BOOL]), Some(("select $1".into(), vec![Type::TEXT])));
    }

    #[test]
    fn connection_local_evictions_remain_bounded_under_churn() {
        for connection_number in 0..100 {
            let mut tracker = BoundedStatementTracker::new(3);
            for statement in 0..100 {
                let evicted = tracker.touch(&format!("select {connection_number}, {statement}"), &[]);
                let expected =
                    (statement >= 3).then(|| (format!("select {connection_number}, {}", statement - 3), vec![]));
                assert_eq!(evicted, expected);
            }
        }
    }

    #[test]
    fn clear_forgets_statements_and_recency() {
        let mut tracker = BoundedStatementTracker::new(2);
        tracker.touch("select 1", &[]);
        tracker.touch("select 2", &[]);
        tracker.clear();
        assert_eq!(tracker.touch("select 3", &[]), None);
        assert_eq!(tracker.touch("select 1", &[]), None);
        assert_eq!(tracker.touch("select 4", &[]), Some(("select 3".into(), vec![])));
    }

    #[test]
    fn zero_capacity_retains_no_statements() {
        let mut tracker = BoundedStatementTracker::new(0);
        for _ in 0..2 {
            assert_eq!(tracker.touch("select $1", &[Type::INT4]), Some(("select $1".into(), vec![Type::INT4])));
        }
        tracker.clear();
        assert_eq!(tracker.touch("select 1", &[]), Some(("select 1".into(), vec![])));
    }

    #[test]
    fn maximum_capacity_does_not_reserve_storage_upfront() {
        let mut tracker = BoundedStatementTracker::new(usize::MAX);
        assert_eq!(tracker.touch("select 1", &[]), None);
        assert_eq!(tracker.touch("select 2", &[]), None);
        assert_eq!(tracker.touch("select 1", &[]), None);
    }
}
