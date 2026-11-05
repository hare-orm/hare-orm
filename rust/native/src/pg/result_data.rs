//! Result rows decoded once before publication, including server-provided opaque text values.

use crate::pg::error::DriverError;
use crate::pg::row_decoder::PgRowDecoder;
use crate::pg::value::Value;
use tokio_postgres::Row;

pub struct ResultData {
    pub rows: Vec<Vec<Value>>,
    pub row_count: Option<u64>,
}

impl ResultData {
    pub async fn read(
        client: &tokio_postgres::Client,
        rows: Vec<Row>,
        row_count: Option<u64>,
    ) -> Result<Self, DriverError> {
        Ok(Self { rows: PgRowDecoder::decode(client, &rows).await?, row_count })
    }

    pub fn from_rows(rows: Vec<Vec<Value>>) -> Self {
        Self { row_count: Some(rows.len() as u64), rows }
    }

    pub fn len(&self) -> usize {
        self.rows.len()
    }
    pub fn is_empty(&self) -> bool {
        self.rows.is_empty()
    }
}
