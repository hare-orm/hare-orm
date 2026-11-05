//! COPY BINARY bulk load, behind `bulk_create(use_copy=True)`. COPY has no per-value type
//! negotiation: the bytes must already match each column's binary format, so the Python side sends
//! each column's base SQL type (`"VARCHAR(255)"` -> `"VARCHAR"`), in column order. Only hare's core
//! scalar types are supported; any other type is refused by name rather than risk a wrong encoding.

use std::pin::pin;

use crate::pg::timed_manager::Object;
use tokio_postgres::binary_copy::BinaryCopyInWriter;
use tokio_postgres::types::Type;

use tokio_postgres::types::ToSql;

use crate::pg::error::DriverError;
use crate::pg::value::{BinaryParameter, Value};

/// The COPY BINARY type of a column of SQL type `base_type_name` - every text type is `TEXT`, whose
/// binary format is plain UTF-8.
fn get_copy_type(base_type_name: &str) -> Result<Type, DriverError> {
    Ok(match base_type_name {
        "SMALLINT" => Type::INT2,
        "INT" | "INTEGER" => Type::INT4,
        "BIGINT" => Type::INT8,
        "REAL" => Type::FLOAT4,
        "DOUBLE PRECISION" => Type::FLOAT8,
        "NUMERIC" | "DECIMAL" => Type::NUMERIC,
        "BOOL" | "BOOLEAN" => Type::BOOL,
        "TEXT" | "VARCHAR" | "CHAR" | "CHARACTER VARYING" | "CHARACTER" => Type::TEXT,
        "UUID" => Type::UUID,
        "DATE" => Type::DATE,
        "TIME" => Type::TIME,
        "TIMETZ" => Type::TIMETZ,
        "TIMESTAMP" => Type::TIMESTAMP,
        "TIMESTAMPTZ" => Type::TIMESTAMPTZ,
        "JSON" => Type::JSON,
        "JSONB" => Type::JSONB,
        "BYTEA" | "BLOB" => Type::BYTEA,
        other => {
            return Err(DriverError::Query(format!(
                "bulk_create(use_copy=True) does not support column type {other:?} on the rust_pg \
                 driver yet - only hare's core scalar field types are supported for COPY \
                 (array/range fields and Postgres-extension field types like citext/PostGIS/ \
                 tsvector are not); use the default multi-row INSERT path (use_copy=False) for \
                 this model instead"
            )));
        }
    })
}

/// Double-quotes an SQL identifier, doubling any `"` in it.
fn quote_ident(name: &str) -> String {
    format!("\"{}\"", name.replace('"', "\"\""))
}

/// Validate the binary frame shape before starting COPY or calling the writer,
/// which panics on mismatched row widths and narrows the field count to i16.
fn check_copy_shape(columns: usize, types: usize, records: &[Vec<Value>]) -> Result<(), DriverError> {
    if columns != types {
        return Err(DriverError::Query(format!("COPY names {columns} columns but declares {types} types")));
    }
    if i16::try_from(columns).is_err() {
        return Err(DriverError::Query("COPY column count exceeds the binary protocol limit".to_string()));
    }
    for (index, record) in records.iter().enumerate() {
        if record.len() != columns {
            return Err(DriverError::Query(format!(
                "COPY row {index} has {} values; expected {columns}",
                record.len()
            )));
        }
    }
    Ok(())
}

pub(crate) async fn copy_in_records(
    client: &Object,
    table: &str,
    schema: Option<&str>,
    columns: &[String],
    column_types: &[String],
    records: Vec<Vec<Value>>,
    tags_comment: &str,
) -> Result<u64, DriverError> {
    check_copy_shape(columns.len(), column_types.len(), &records)?;
    let types: Vec<Type> =
        column_types.iter().map(|column_type| get_copy_type(column_type)).collect::<Result<_, _>>()?;
    let quoted_columns = columns.iter().map(|column| quote_ident(column)).collect::<Vec<_>>().join(", ");
    // The query tags end the statement as a comment, as they end every other one.
    let target = match schema {
        Some(schema) => format!("{}.{}", quote_ident(schema), quote_ident(table)),
        None => quote_ident(table),
    };
    let sql = format!("COPY {target} ({quoted_columns}) FROM STDIN BINARY{tags_comment}");

    let sink = client.copy_in(&sql).await?;
    let mut writer = pin!(BinaryCopyInWriter::new(sink, &types));
    for row in &records {
        let parameters: Vec<BinaryParameter> = row.iter().map(BinaryParameter).collect();
        let parameter_refs: Vec<&(dyn ToSql + Sync)> =
            parameters.iter().map(|parameter| parameter as &(dyn ToSql + Sync)).collect();
        writer.as_mut().write(&parameter_refs).await?;
    }
    Ok(writer.finish().await?)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn copy_rejects_inconsistent_metadata_and_rows_before_writing() {
        assert!(check_copy_shape(2, 1, &[]).is_err());
        assert!(check_copy_shape(32_768, 32_768, &[]).is_err());
        assert!(check_copy_shape(2, 2, &[vec![Value::Int(1)]]).is_err());
        assert!(check_copy_shape(1, 1, &[vec![Value::Null], vec![Value::Int(2), Value::Int(3)]]).is_err());
        check_copy_shape(2, 2, &[vec![Value::Int(1), Value::Null], vec![Value::Int(2), Value::Int(3)]]).unwrap();
        check_copy_shape(2, 2, &[]).unwrap();
    }
}
