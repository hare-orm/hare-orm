//! Resolve text-array metadata from actual prepared types on the executing connection.

use std::collections::{HashMap, HashSet};
use std::sync::PoisonError;

use tokio_postgres::types::Kind as TypeShape;
use tokio_postgres::Statement;

use crate::pg::bound_parameter::PgBoundParameter;
use crate::pg::error::DriverError;
use crate::pg::timed_manager::Object;
use crate::pg::types::array::is_text_format_array;
use crate::pg::value::{domain_base_type, Value};

pub(crate) struct PgParameterDescription {
    // Empty for ordinary parameters: no allocation or catalog access on that path.
    text_array_delimiters: Vec<Option<u8>>,
}

impl PgParameterDescription {
    /// Scan every batch row before execution: its first value may be NULL or an empty array.
    /// Member domains retain their own OID, whose typdelim governs their array input syntax.
    pub(crate) async fn for_rows<'a>(
        client: &Object,
        statement: &Statement,
        rows: impl IntoIterator<Item = &'a [Value]>,
    ) -> Result<Self, DriverError> {
        let types = statement.params();
        let mut members = Vec::new();
        for row in rows {
            if row.len() != types.len() {
                return Err(DriverError::Conversion(format!(
                    "expected {} parameters, received {}",
                    types.len(),
                    row.len()
                )));
            }
            for (position, (value, postgres_type)) in row.iter().zip(types).enumerate() {
                if is_text_format_array(value, postgres_type) {
                    let TypeShape::Array(member) = domain_base_type(postgres_type).kind() else {
                        unreachable!("text-array selection requires an array type")
                    };
                    if members.is_empty() {
                        members.resize(types.len(), None);
                    }
                    members[position] = Some(member.oid());
                }
            }
        }
        if members.is_empty() {
            return Ok(Self { text_array_delimiters: Vec::new() });
        }
        // The current bind must remain complete even when it exceeds cache capacity.
        let mut delimiters = HashMap::new();
        let missing: Vec<u32> = {
            let mut cached =
                client.text_array_delimiters.as_ref().map(|cache| cache.lock().unwrap_or_else(PoisonError::into_inner));
            members
                .iter()
                .flatten()
                .copied()
                .filter(|oid| {
                    if let Some(delimiter) = cached.as_mut().and_then(|cache| cache.get(oid)).copied() {
                        delimiters.insert(*oid, delimiter);
                        false
                    } else {
                        true
                    }
                })
                .collect::<HashSet<_>>()
                .into_iter()
                .collect()
        };
        if !missing.is_empty() {
            let rows = client
                .query("SELECT oid,typdelim FROM pg_catalog.pg_type WHERE oid=ANY($1::oid[])", &[&missing])
                .await?;
            let mut loaded = HashMap::new();
            for row in rows {
                let oid: u32 = row.try_get(0)?;
                let delimiter: i8 = row.try_get(1)?;
                if !missing.contains(&oid) || loaded.insert(oid, delimiter as u8).is_some() {
                    return Err(DriverError::Conversion("unexpected array delimiter metadata".to_string()));
                }
            }
            if loaded.len() != missing.len() {
                return Err(DriverError::Conversion("array delimiter metadata is unavailable".to_string()));
            }
            if let Some(cache) = &client.text_array_delimiters {
                let mut cache = cache.lock().unwrap_or_else(PoisonError::into_inner);
                for (&oid, &delimiter) in &loaded {
                    cache.put(oid, delimiter);
                }
            }
            delimiters.extend(loaded);
        }
        let text_array_delimiters = members.into_iter().map(|oid| oid.map(|oid| delimiters[&oid])).collect();
        Ok(Self { text_array_delimiters })
    }

    pub(crate) fn bind<'a>(&'a self, params: &'a [Value]) -> impl ExactSizeIterator<Item = PgBoundParameter<'a>> + 'a {
        params.iter().enumerate().map(|(position, value)| PgBoundParameter {
            value,
            text_array_delimiter: self.text_array_delimiters.get(position).copied().flatten(),
        })
    }
}
