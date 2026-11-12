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
        let mut forms = ServerTextForms::collecting();
        let decoders: Vec<_> = rows
            .first()
            .map_or(&[][..], |row| row.columns())
            .iter()
            .map(|column| PgColumnDecoder::new(column.type_()))
            .collect();
        let mut decoded =
            rows.iter().map(|row| decode_pg_row_values(row, &decoders, &mut forms)).collect::<Result<Vec<_>, _>>()?;
        let requests = forms.take_requests();
        if requests.is_empty() {
            return Ok(decoded);
        }
        let texts = Self::fetch_server_text_forms(client, &requests).await?;
        ServerTextForms::supply(&mut decoded, texts).map_err(|error| DriverError::Conversion(error.to_string()))?;
        Ok(decoded)
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
