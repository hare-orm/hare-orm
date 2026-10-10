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
fn resolve_copy_type(base_type_name: &str) -> Result<Type, DriverError> {
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

pub(crate) async fn copy_in_records(
    client: &Object,
    table: &str,
    columns: &[String],
    column_types: &[String],
    records: Vec<Vec<Value>>,
) -> Result<u64, DriverError> {
    let types: Vec<Type> =
        column_types.iter().map(|column_type| resolve_copy_type(column_type)).collect::<Result<_, _>>()?;
    let quoted_columns = columns.iter().map(|column| quote_ident(column)).collect::<Vec<_>>().join(", ");
    let sql = format!("COPY {} ({}) FROM STDIN BINARY", quote_ident(table), quoted_columns);

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
