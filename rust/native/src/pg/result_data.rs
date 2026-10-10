//! `ResultData` - the rows of a result: as they came off the wire, every value checked to decode
//! but none decoded yet, or decoded - a result holding a value of a type only the server can turn
//! into text, which needs another query to read.

use tokio_postgres::Row;

use crate::pg::client::decode_rows;
use crate::pg::error::DriverError;
use crate::pg::result_cell::ResultCell;
use crate::pg::value::{check_pg_row_values, get_raw_column, type_may_need_server_text_form, Value};

pub enum ResultData {
    Wire(Vec<Row>),
    Values(Vec<Vec<Value>>),
}

impl ResultData {
    /// The data of `rows`, read on `client` - which asks the server for the text form of a value
    /// no decoder here reads.
    pub async fn read(client: &tokio_postgres::Client, rows: Vec<Row>) -> Result<Self, DriverError> {
        let needs_server_text_forms = rows
            .first()
            .is_some_and(|row| row.columns().iter().any(|column| type_may_need_server_text_form(column.type_())));
        if needs_server_text_forms {
            return decode_rows(client, &rows).await.map(ResultData::Values);
        }
        for row in &rows {
            check_pg_row_values(row)?;
        }
        Ok(ResultData::Wire(rows))
    }

    pub fn len(&self) -> usize {
        match self {
            ResultData::Wire(rows) => rows.len(),
            ResultData::Values(rows) => rows.len(),
        }
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// How many columns row `row` has.
    pub fn get_width(&self, row: usize) -> usize {
        match self {
            ResultData::Wire(rows) => rows.get(row).map_or(0, Row::len),
            ResultData::Values(rows) => rows.get(row).map_or(0, Vec::len),
        }
    }

    /// The value of column `column` of row `row` - None past the last row or column.
    pub fn get_cell(&self, row: usize, column: usize) -> Option<ResultCell<'_>> {
        match self {
            ResultData::Wire(rows) => {
                let row = rows.get(row)?;
                let postgres_type = row.columns().get(column)?.type_();
                // Reading the bytes of an existing column never fails.
                Some(ResultCell::Wire { postgres_type, raw: get_raw_column(row, column).ok()? })
            }
            ResultData::Values(rows) => rows.get(row)?.get(column).map(ResultCell::Value),
        }
    }
}
