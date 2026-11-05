use crate::pg::error::DriverError;
use crate::pg::parameter_values::ParameterValues;
use crate::pg::timed_manager::Object;
use crate::pg::value::Value;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::sync::PoisonError;
use tokio_postgres::types::Type;
use tokio_postgres::Statement;

/// How a connection caches prepared statements: `enabled` chooses `prepare_typed_cached` over
/// `prepare_typed` (`statement_cache_size=0`, e.g. behind PgBouncer in transaction pooling);
/// The physical connection retains its bounded statement order.
#[derive(Clone)]
pub(crate) struct StatementCaching {
    pub(crate) enabled: bool,
    schema_generation: Arc<AtomicU64>,
}

impl StatementCaching {
    pub(super) fn from_configured_size(statement_cache_size: Option<usize>) -> Self {
        Self { enabled: statement_cache_size != Some(0), schema_generation: Arc::new(AtomicU64::new(0)) }
    }

    /// All connections of this pool discard metadata before their next use, including pinned ones.
    pub(crate) fn invalidate(&self) {
        self.schema_generation.fetch_add(1, Ordering::AcqRel);
    }

    pub(crate) fn refresh(&self, client: &Object) {
        let generation = self.schema_generation.load(Ordering::Acquire);
        if client.schema_generation.load(Ordering::Acquire) == generation {
            return;
        }
        client.statement_cache.clear();
        client.clear_type_cache();
        if let Some(tracker) = &client.statement_order {
            tracker.lock().unwrap_or_else(PoisonError::into_inner).clear();
        }
        client.schema_generation.store(generation, Ordering::Release);
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
        if let Some(tracker) = &client.statement_order {
            if let Some((evicted_sql, evicted_types)) =
                tracker.lock().unwrap_or_else(PoisonError::into_inner).touch(sql, types)
            {
                client.statement_cache.remove(&evicted_sql, &evicted_types);
            }
        }
        Ok(statement)
    }
    pub(crate) async fn prepare_values(
        &self,
        client: &Object,
        sql: &str,
        params: &[Value],
    ) -> Result<Statement, DriverError> {
        let types = ParameterValues::types(params);
        Ok(self.prepare(client, sql, &types).await?)
    }

    /// Invalidate only PostgreSQL's cached-plan result-shape failure, not arbitrary 0A000 errors.
    fn is_stale_cached_plan_error(error: &tokio_postgres::Error) -> bool {
        error
            .as_db_error()
            .is_some_and(|db| db.code().code() == "0A000" && db.routine() == Some("RevalidateCachedQuery"))
    }

    /// Prepare and execute once. A stale plan is evicted for the next explicit call.
    /// tokio-postgres does not identify whether an error came from the outer Bind or a nested
    /// statement during Execute; even this server routine does not prove that no effects occurred.
    pub(crate) async fn run<T, F, Fut>(
        &self,
        client: &Object,
        sql: &str,
        params: &[Value],
        run: F,
    ) -> Result<(Statement, T), DriverError>
    where
        F: FnOnce(Statement) -> Fut,
        Fut: std::future::Future<Output = Result<T, tokio_postgres::Error>>,
    {
        let types = ParameterValues::types(params);
        let statement = self.prepare(client, sql, &types).await?;
        match run(statement.clone()).await {
            Ok(value) => Ok((statement, value)),
            Err(error) if self.enabled && Self::is_stale_cached_plan_error(&error) => {
                client.statement_cache.remove(sql, &types);
                Err(error.into())
            }
            Err(error) => Err(DriverError::from(error)),
        }
    }
}
