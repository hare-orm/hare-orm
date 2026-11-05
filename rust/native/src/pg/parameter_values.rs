//! Parameters of one statement, converted from Python values at the driver boundary.

use crate::pg::error::DriverError;
use pyo3::prelude::*;
use pyo3::Borrowed;
use std::sync::OnceLock;
use tokio_postgres::types::{Kind as TypeShape, ToSql, Type};

use crate::pg::value::Value;

pub struct ParameterValues(pub Vec<Value>);

impl<'a, 'py> FromPyObject<'a, 'py> for ParameterValues {
    type Error = PyErr;

    fn extract(parameters: Borrowed<'a, 'py, PyAny>) -> Result<Self, PyErr> {
        Ok(ParameterValues(parameters.extract::<Vec<Value>>()?))
    }
}

impl ParameterValues {
    /// The unspecified parameter type (OID 0) - the server infers the parameter's type from context.
    /// Built once: `Type::new()` allocates, and most bind parameters need this type.
    pub(crate) fn unspecified_type() -> Type {
        static UNSPECIFIED: OnceLock<Type> = OnceLock::new();
        UNSPECIFIED.get_or_init(|| Type::new(String::new(), 0, TypeShape::Simple, String::new())).clone()
    }

    pub(crate) fn as_sql_refs(params: &[Value]) -> Vec<&(dyn ToSql + Sync)> {
        params.iter().map(|value| value as &(dyn ToSql + Sync)).collect()
    }

    pub(crate) fn types(params: &[Value]) -> Vec<Type> {
        params.iter().map(|value| value.pg_type().unwrap_or_else(Self::unspecified_type)).collect()
    }

    /// The parameter types of an `execute_many` batch, one per column for all rows: that of the first
    /// value with a declared type (`Value::pg_type()`), widened when rows disagree (`widen_pg_type()`); a
    /// batch mixing types that can't widen to one is refused. Adapted from yara-orm's
    /// `unified_param_types` (MIT License).
    pub(crate) fn batch_types(rows: &[Vec<Value>]) -> Result<Vec<Type>, DriverError> {
        let Some(first) = rows.first() else {
            return Ok(Vec::new());
        };
        let mut types: Vec<Option<Type>> = first.iter().map(Value::pg_type).collect();
        for row in &rows[1..] {
            for (index, value) in row.iter().enumerate().take(types.len()) {
                let Some(next) = value.pg_type() else {
                    continue;
                };
                match &mut types[index] {
                    slot @ None => *slot = Some(next),
                    Some(current) if *current == next => {}
                    Some(current) => match Self::widen_pg_type(current, &next) {
                        Some(widened) => *current = widened,
                        None => {
                            return Err(DriverError::Query(format!(
                                "execute_many parameter {} mixes incompatible types across rows ({} vs {})",
                                index + 1,
                                current.name(),
                                next.name(),
                            )))
                        }
                    },
                }
            }
        }
        Ok(types.into_iter().map(|postgres_type| postgres_type.unwrap_or_else(Self::unspecified_type)).collect())
    }

    fn widen_pg_type(first_type: &Type, second_type: &Type) -> Option<Type> {
        match (first_type, second_type) {
            (&Type::TIMESTAMP, &Type::TIMESTAMPTZ) | (&Type::TIMESTAMPTZ, &Type::TIMESTAMP) => {
                return Some(Type::TIMESTAMPTZ)
            }
            (&Type::TEXT, &Type::UUID) | (&Type::UUID, &Type::TEXT) => return Some(Type::UUID),
            _ => {}
        }
        let numeric = |postgres_type: &Type| matches!(*postgres_type, Type::INT8 | Type::FLOAT8 | Type::NUMERIC);
        if numeric(first_type) && numeric(second_type) {
            return Some(Type::NUMERIC);
        }
        None
    }
}
