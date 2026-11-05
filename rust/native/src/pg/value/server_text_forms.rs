//! Deferred server output for types without a binary decoder. SQL NULL never creates a request.

use tokio_postgres::types::Type;

use crate::pg::value::{BoxError, Value};

pub(crate) struct ServerTextForms<'a> {
    requests: Vec<(Type, &'a [u8])>,
}

impl<'a> ServerTextForms<'a> {
    pub(crate) fn collecting() -> Self {
        Self { requests: Vec::new() }
    }

    pub(crate) fn take_requests(&mut self) -> Vec<(Type, &'a [u8])> {
        std::mem::take(&mut self.requests)
    }

    pub(crate) fn text_form(&mut self, postgres_type: &Type, raw: &'a [u8]) -> Value {
        let index = self.requests.len();
        self.requests.push((postgres_type.clone(), raw));
        Value::DeferredText(index)
    }

    /// Replace only deferred leaves; all binary values and their validation are retained.
    /// Each server output must match exactly one request before any result is published.
    pub(crate) fn supply(rows: &mut [Vec<Value>], texts: Vec<String>) -> Result<(), BoxError> {
        let mut texts: Vec<_> = texts.into_iter().map(Some).collect();
        let mut pending: Vec<_> = rows.iter_mut().flatten().collect();
        while let Some(value) = pending.pop() {
            match value {
                Value::DeferredText(index) => {
                    let text = texts
                        .get_mut(*index)
                        .and_then(Option::take)
                        .ok_or_else(|| format!("missing server text form for value {index}"))?;
                    *value = Value::Text(text);
                }
                Value::Array(items) | Value::Composite(items) => pending.extend(items.iter_mut()),
                Value::Range(range) => {
                    pending.extend(range.lower.as_deref_mut());
                    pending.extend(range.upper.as_deref_mut());
                }
                _ => {}
            }
        }
        if texts.iter().any(Option::is_some) {
            return Err("unexpected extra server text forms".into());
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::pg::value::RangeValue;

    #[test]
    fn supply_fills_nested_leaves_without_replacing_null_or_known_values() {
        let mut rows = vec![vec![Value::Composite(vec![
            Value::Null,
            Value::Int(7),
            Value::Array(vec![Value::DeferredText(0)]),
            Value::Range(RangeValue {
                lower: Some(Box::new(Value::DeferredText(1))),
                upper: None,
                lower_inc: true,
                upper_inc: false,
                empty: false,
            }),
        ])]];
        ServerTextForms::supply(&mut rows, vec!["opaque".to_string(), "bound".to_string()]).unwrap();
        let Value::Composite(items) = &rows[0][0] else { panic!("expected record") };
        assert!(matches!(items[0], Value::Null));
        assert!(matches!(items[1], Value::Int(7)));
        assert!(matches!(&items[2], Value::Array(array) if matches!(&array[0], Value::Text(text) if text == "opaque")));
        let Value::Range(range) = &items[3] else { panic!("expected range") };
        assert!(matches!(range.lower.as_deref(), Some(Value::Text(text)) if text == "bound"));
        assert!(range.upper.is_none());
    }

    #[test]
    fn missing_extra_and_repeated_server_text_slots_are_errors() {
        assert!(ServerTextForms::supply(&mut [vec![Value::DeferredText(0)]], vec![]).is_err());
        assert!(ServerTextForms::supply(&mut [vec![Value::Null]], vec!["extra".to_string()]).is_err());
        assert!(ServerTextForms::supply(
            &mut [vec![Value::DeferredText(0), Value::DeferredText(0)]],
            vec!["once".to_string()],
        )
        .is_err());
    }
}
