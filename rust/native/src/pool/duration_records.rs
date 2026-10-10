//! `DurationRecords` - the latest durations of one type a native pool measured (its waits for a connection,
//! its connects), numbered in the order they came. The Rust side of hare's
//! `hare.instrumentation.duration_records.DurationRecords`: each reader keeps its own cursor and takes
//! the durations after it, so readers never take them from one another.

/// The latest `capacity` durations, each with its number (`count` is the number the next one gets).
pub(crate) struct DurationRecords {
    durations: Vec<f64>,
    count: u64,
}

impl DurationRecords {
    pub(crate) fn new(capacity: usize) -> Self {
        Self { durations: vec![0.0; capacity.max(1)], count: 0 }
    }

    /// Adds a duration, in seconds.
    pub(crate) fn add(&mut self, seconds: f64) {
        let capacity = self.durations.len() as u64;
        // The remainder of a division by the capacity always fits the vector's index.
        #[allow(clippy::cast_possible_truncation, reason = "the remainder is below the vector's length")]
        let index = (self.count % capacity) as usize;
        self.durations[index] = seconds;
        self.count += 1;
    }

    /// The durations added after `cursor`: the reader's new cursor, the durations and how many were
    /// already overwritten.
    pub(crate) fn get_since(&self, cursor: u64) -> (u64, Vec<f64>, u64) {
        let capacity = self.durations.len() as u64;
        let first = cursor.max(self.count.saturating_sub(capacity));
        #[allow(clippy::cast_possible_truncation, reason = "the remainder is below the vector's length")]
        let durations = (first..self.count).map(|number| self.durations[(number % capacity) as usize]).collect();
        (self.count, durations, first.saturating_sub(cursor))
    }
}

#[cfg(test)]
mod tests {
    use super::DurationRecords;

    #[test]
    fn a_reader_takes_the_durations_after_its_cursor() {
        let mut records = DurationRecords::new(4);
        records.add(1.0);
        records.add(2.0);
        assert_eq!(records.get_since(0), (2, vec![1.0, 2.0], 0));
        assert_eq!(records.get_since(1), (2, vec![2.0], 0));
        assert_eq!(records.get_since(2), (2, vec![], 0));
    }

    #[test]
    fn a_reader_behind_the_capacity_gets_the_count_it_missed() {
        let mut records = DurationRecords::new(2);
        for seconds in [1.0, 2.0, 3.0, 4.0, 5.0] {
            records.add(seconds);
        }
        assert_eq!(records.get_since(0), (5, vec![4.0, 5.0], 3));
        assert_eq!(records.get_since(4), (5, vec![5.0], 0));
    }

    #[test]
    fn a_cursor_past_the_count_takes_nothing() {
        let mut records = DurationRecords::new(2);
        records.add(1.0);
        assert_eq!(records.get_since(7), (1, vec![], 0));
    }
}
