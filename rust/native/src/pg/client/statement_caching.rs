use crate::pg::bounded_statement_tracker::BoundedStatementTracker;
use crate::pg::timed_manager::Object;
use std::sync::Arc;
use tokio_postgres::types::Type;
use tokio_postgres::Statement;

/// How a connection caches prepared statements: `enabled` chooses `prepare_typed_cached` over
/// `prepare_typed` (`statement_cache_size=0`, e.g. behind PgBouncer in transaction pooling);
/// `limiter` bounds the statements cached per physical connection (`statement_cache_size=<n>`).
#[derive(Clone)]
pub(crate) struct StatementCaching {
    pub(crate) enabled: bool,
    pub(crate) limiter: Option<Arc<BoundedStatementTracker<deadpool_postgres::StatementCache>>>,
}

impl StatementCaching {
    pub(super) fn from_configured_size(statement_cache_size: Option<usize>) -> Self {
        match statement_cache_size {
            Some(0) => Self { enabled: false, limiter: None },
            Some(limit) => Self { enabled: true, limiter: Some(Arc::new(BoundedStatementTracker::new(limit))) },
            None => Self { enabled: true, limiter: None },
        }
    }

    /// Prepares `sql` with parameter types `types` on `client`, through the statement cache when
    /// enabled.
    pub(crate) async fn prepare(
        &self,
        client: &Object,
        sql: &str,
        types: &[Type],
    ) -> Result<Statement, tokio_postgres::Error> {
        if !self.enabled {
            return client.prepare_typed(sql, types).await;
        }
        let statement = client.prepare_typed_cached(sql, types).await?;
        if let Some(limiter) = &self.limiter {
            if let Some((evicted_sql, evicted_types)) = limiter.touch(&client.statement_cache, sql, types) {
                client.statement_cache.remove(&evicted_sql, &evicted_types);
            }
        }
        Ok(statement)
    }
}
