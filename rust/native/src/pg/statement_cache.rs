//! An entry limit for `deadpool_postgres::StatementCache`, which has none: every distinct SQL text a
//! connection prepares (each chunk size of a chunked `bulk_create`, say) would stay a server-side
//! prepared statement for the connection's lifetime. [`BoundedStatementTracker`] keeps the least
//! recently used order per physical connection - keyed by the address of its `Arc<StatementCache>`,
//! the same across every checkout of that connection - and names the entry to evict.
//!
//! Generic over the connection identity `T` so tests can use `()`: `StatementCache` has no public
//! constructor. `T` is used only for `Arc` pointer identity.

use std::collections::{HashMap, VecDeque};
use std::sync::{Arc, Mutex, Weak};

use tokio_postgres::types::Type;

/// One physical connection's least recently used order and the connection it was recorded for.
struct ConnectionEntry<T> {
    identity: Weak<T>,
    order: VecDeque<(String, Vec<Type>)>,
}

/// The least recently used order of every connection of one pool, enforcing `statement_cache_size`.
pub(crate) struct BoundedStatementTracker<T> {
    limit: usize,
    connections: Mutex<HashMap<usize, ConnectionEntry<T>>>,
}

impl<T> BoundedStatementTracker<T> {
    pub(crate) fn new(limit: usize) -> Self {
        Self { limit, connections: Mutex::new(HashMap::new()) }
    }

    /// Records `sql`/`types` as just prepared on `connection` and returns the least recently used
    /// entry to evict once the connection holds more than the limit - at most one per call.
    pub(crate) fn touch(&self, connection: &Arc<T>, sql: &str, types: &[Type]) -> Option<(String, Vec<Type>)> {
        let address = Arc::as_ptr(connection) as usize;
        let mut connections = self.connections.lock().unwrap();
        let entry = connections
            .entry(address)
            .or_insert_with(|| ConnectionEntry { identity: Arc::downgrade(connection), order: VecDeque::new() });
        // A later connection took the address of a dropped one: its order starts empty.
        let stale = match entry.identity.upgrade() {
            Some(previous) => !Arc::ptr_eq(&previous, connection),
            None => true,
        };
        if stale {
            entry.identity = Arc::downgrade(connection);
            entry.order.clear();
        }
        if let Some(pos) = entry.order.iter().position(|(key, key_types)| key == sql && key_types == types) {
            entry.order.remove(pos);
        }
        entry.order.push_back((sql.to_owned(), types.to_owned()));
        if entry.order.len() > self.limit {
            entry.order.pop_front()
        } else {
            None
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// `()` stands in for `StatementCache` - only the `Arc` address is used.
    fn connection() -> Arc<()> {
        Arc::new(())
    }

    #[test]
    fn no_eviction_while_at_or_under_the_limit() {
        let tracker = BoundedStatementTracker::new(2);
        let connection = connection();
        assert_eq!(tracker.touch(&connection, "select 1", &[]), None);
        assert_eq!(tracker.touch(&connection, "select 2", &[]), None);
    }

    #[test]
    fn evicts_the_least_recently_used_entry_once_over_the_limit() {
        let tracker = BoundedStatementTracker::new(2);
        let connection = connection();
        tracker.touch(&connection, "select 1", &[]);
        tracker.touch(&connection, "select 2", &[]);
        let evicted = tracker.touch(&connection, "select 3", &[]);
        assert_eq!(evicted, Some(("select 1".to_string(), vec![])));
    }

    #[test]
    fn re_touching_an_existing_key_moves_it_to_most_recently_used() {
        let tracker = BoundedStatementTracker::new(2);
        let connection = connection();
        tracker.touch(&connection, "select 1", &[]);
        tracker.touch(&connection, "select 2", &[]);
        tracker.touch(&connection, "select 1", &[]); // "select 2" is now the LRU one
        let evicted = tracker.touch(&connection, "select 3", &[]);
        assert_eq!(evicted, Some(("select 2".to_string(), vec![])));
    }

    #[test]
    fn distinct_types_for_the_same_sql_text_are_distinct_keys() {
        let tracker = BoundedStatementTracker::new(2);
        let connection = connection();
        tracker.touch(&connection, "select $1", &[Type::INT4]);
        tracker.touch(&connection, "select $1", &[Type::TEXT]);
        // Re-touching one of the two (by types) must not evict the other one below.
        tracker.touch(&connection, "select $1", &[Type::INT4]);
        let evicted = tracker.touch(&connection, "select $1", &[Type::BOOL]);
        assert_eq!(evicted, Some(("select $1".to_string(), vec![Type::TEXT])));
    }

    #[test]
    fn different_connections_are_tracked_independently() {
        let tracker = BoundedStatementTracker::new(1);
        let connection_a = connection();
        let connection_b = connection();
        assert_eq!(tracker.touch(&connection_a, "select a", &[]), None);
        assert_eq!(tracker.touch(&connection_b, "select b", &[]), None);
        let tracker_state = tracker.connections.lock().unwrap();
        assert_eq!(tracker_state.len(), 2);
    }

    #[test]
    fn a_reused_address_from_a_dropped_connection_discards_the_stale_order() {
        let tracker = BoundedStatementTracker::new(5);
        let address;
        {
            let connection = connection();
            address = Arc::as_ptr(&connection) as usize;
            tracker.touch(&connection, "select 1", &[]);
            tracker.touch(&connection, "select 2", &[]);
        } // `connection` (and its allocation) is dropped here.

        // The allocator may or may not hand the freed address out again soon.
        let mut reused = None;
        for _ in 0..10_000 {
            let candidate = connection();
            if Arc::as_ptr(&candidate) as usize == address {
                reused = Some(candidate);
                break;
            }
        }
        let Some(new_connection) = reused else {
            // The address was never reused in this run - nothing to check.
            return;
        };
        let evicted = tracker.touch(&new_connection, "select fresh", &[]);
        assert_eq!(evicted, None, "the old connection's order must not leak into the new one");
        let tracker_state = tracker.connections.lock().unwrap();
        let entry = &tracker_state[&address];
        assert_eq!(entry.order.len(), 1);
        assert!(entry.order.iter().any(|(sql, _)| sql == "select fresh"));
    }
}
