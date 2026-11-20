//! Decode one result batch and fill opaque values using the server's output functions.
use std::fmt::Write as _;

use tokio_postgres::types::{ToSql, Type};

use crate::pg::error::DriverError;
use crate::pg::value::column_decoder::PgColumnDecoder;
use crate::pg::value::{decode_pg_row_values, RawWireValue, ServerTextForms, Value};

const SERVER_TEXT_FORMS_PER_QUERY: usize = 10_000;
pub(crate) struct PgRowDecoder;
impl PgRowDecoder {
    /// Decodes result rows, asking the server (on the same connection) for the text form of every
    /// value of a type this driver has no binary decoder for - see `ServerTextForms`.
    pub(crate) async fn decode(
        client: &tokio_postgres::Client,
        rows: &[tokio_postgres::Row],
    ) -> Result<Vec<Vec<Value>>, DriverError> {
        let decoders: Vec<_> = rows
            .first()
            .map_or(&[][..], |row| row.columns())
            .iter()
            .map(|column| PgColumnDecoder::new(column.type_()))
            .collect();
        let mut field_types = Vec::new();
        loop {
            let mut forms = ServerTextForms::with_field_types(&field_types);
            let mut decoded = rows
                .iter()
                .map(|row| decode_pg_row_values(row, &decoders, &mut forms))
                .collect::<Result<Vec<_>, _>>()?;
            let requests = forms.take_requests();
            let mut missing_types: Vec<_> = requests
                .iter()
                .filter(|(postgres_type, _)| postgres_type.name().is_empty())
                .map(|(postgres_type, _)| postgres_type.clone())
                .collect();
            if !missing_types.is_empty() {
                missing_types.sort_unstable_by_key(Type::oid);
                missing_types.dedup_by_key(|postgres_type| postgres_type.oid());
                drop(requests);
                drop(forms);
                field_types.extend(Self::get_field_types(client, &missing_types).await?);
                // Decode the original rows again, retaining the ordinary depth and size accounting.
                continue;
            }
            if !requests.is_empty() {
                let texts = Self::fetch_server_text_forms(client, &requests).await?;
                ServerTextForms::supply(&mut decoded, texts)
                    .map_err(|error| DriverError::Conversion(error.to_string()))?;
            }
            return Ok(decoded);
        }
    }

    /// Let tokio-postgres obtain domains, arrays, ranges and named composites through its type cache.
    async fn get_field_types(client: &tokio_postgres::Client, missing: &[Type]) -> Result<Vec<Type>, DriverError> {
        let mut field_types = Vec::with_capacity(missing.len());
        for chunk in missing.chunks(SERVER_TEXT_FORMS_PER_QUERY) {
            let mut sql = String::from("SELECT ");
            for index in 1..=chunk.len() {
                if index != 1 {
                    sql.push(',');
                }
                write!(sql, "${index}").expect("writing into a String never fails");
            }
            let statement = client.prepare_typed(&sql, chunk).await?;
            if statement.params().iter().any(|postgres_type| postgres_type.name().is_empty()) {
                return Err(DriverError::Conversion("PostgreSQL did not supply a record field's type metadata".into()));
            }
            field_types.extend_from_slice(statement.params());
        }
        Ok(field_types)
    }

    /// Sends raw binary values back as parameters of their own type cast to text - the type's own
    /// output function, exactly what a text-format result would have carried.
    async fn fetch_server_text_forms(
        client: &tokio_postgres::Client,
        requests: &[(Type, &[u8])],
    ) -> Result<Vec<String>, DriverError> {
        let mut texts = Vec::with_capacity(requests.len());
        for chunk in requests.chunks(SERVER_TEXT_FORMS_PER_QUERY) {
            let mut sql = String::from("SELECT ARRAY[");
            for index in 1..=chunk.len() {
                if index != 1 {
                    sql.push(',');
                }
                write!(sql, "${index}::text").expect("writing into a String never fails");
            }
            sql.push_str("]::text[]");
            let raw_values: Vec<RawWireValue> = chunk.iter().map(|(_, raw)| RawWireValue(raw)).collect();
            let params: Vec<(&(dyn ToSql + Sync), Type)> = raw_values
                .iter()
                .zip(chunk)
                .map(|(raw_value, (postgres_type, _))| (raw_value as &(dyn ToSql + Sync), postgres_type.clone()))
                .collect();
            let rows = client.query_typed(&sql, &params).await?;
            let row =
                rows.first().ok_or_else(|| DriverError::Conversion("text-form query returned no row".to_string()))?;
            texts.extend(row.try_get::<_, Vec<String>>(0)?);
        }
        Ok(texts)
    }
}
