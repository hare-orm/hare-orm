//! One parameter with the physical connection's native text-array delimiter.

use bytes::BytesMut;
use tokio_postgres::types::{to_sql_checked, Format, IsNull, ToSql, Type};

use crate::pg::parameter_encoding::{PgParameterEncoding, PgParameterMode};
use crate::pg::value::{BoxError, Value};

#[derive(Debug)]
pub(crate) struct PgBoundParameter<'a> {
    pub(crate) value: &'a Value,
    pub(crate) text_array_delimiter: Option<u8>,
}

impl ToSql for PgBoundParameter<'_> {
    fn to_sql(&self, postgres_type: &Type, out: &mut BytesMut) -> Result<IsNull, BoxError> {
        PgParameterEncoding::new(self.value, postgres_type, PgParameterMode::Negotiated)
            .with_text_array_delimiter(self.text_array_delimiter)
            .encode(out)
    }

    fn encode_format(&self, postgres_type: &Type) -> Format {
        PgParameterEncoding::new(self.value, postgres_type, PgParameterMode::Negotiated)
            .with_text_array_delimiter(self.text_array_delimiter)
            .format()
    }

    fn accepts(_postgres_type: &Type) -> bool {
        true
    }

    to_sql_checked!();
}
