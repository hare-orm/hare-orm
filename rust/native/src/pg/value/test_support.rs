//! Helpers the value and type tests share.

use bytes::BytesMut;
use tokio_postgres::types::{Kind as TypeShape, ToSql, Type};

use crate::pg::value::decode::{decode_value, ServerTextForms};
use crate::pg::value::Value;

pub(crate) fn encode(value: &Value, ty: &Type) -> Result<Vec<u8>, String> {
    let mut out = BytesMut::new();
    value.to_sql(ty, &mut out).map(|_| out.to_vec()).map_err(|e| e.to_string())
}

pub(crate) fn decode(ty: &Type, raw: &[u8]) -> Value {
    decode_value(ty, raw, &mut ServerTextForms::collecting()).expect("well-formed payload must decode")
}

pub(crate) fn vector_type() -> Type {
    Type::new("vector".to_string(), 0, TypeShape::Simple, "public".to_string())
}

pub(crate) fn vector_array_type() -> Type {
    Type::new("_vector".to_string(), 16400, TypeShape::Array(vector_type()), "public".to_string())
}
