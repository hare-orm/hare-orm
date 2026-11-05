//! TSQUERY decoded into a flat node table and rendered without recursive native calls.

use crate::pg::decode_context::PgDecodeContext;
use crate::pg::value::BoxError;
use crate::pg::wire_reader::PgWireReader;
use std::fmt::Write as _;
use tokio_postgres::types::{FromSql, Type};

enum TsQueryNode {
    Operand { lexeme: String, weight: u8, prefix: bool },
    Not(usize),
    Binary { operator: u8, distance: i16, left: usize, right: usize },
}

enum RenderStep {
    Node { index: usize, parent_priority: i32, right_phrase: bool },
    Text(&'static str),
    Operator { operator: u8, distance: i16 },
}

/// The text PostgreSQL prints for a TSQUERY value.
pub(crate) struct PgTsQueryText(pub(crate) String);

impl PgTsQueryText {
    const ITEM_OPERAND: u8 = 1;
    const ITEM_OPERATOR: u8 = 2;
    const OPERATOR_NOT: u8 = 1;
    const OPERATOR_AND: u8 = 2;
    const OPERATOR_OR: u8 = 3;
    const OPERATOR_PHRASE: u8 = 4;

    fn read_nodes(reader: &mut PgWireReader<'_>, count: usize) -> Result<Vec<TsQueryNode>, BoxError> {
        let mut nodes = Vec::with_capacity(count);
        let mut pending: Vec<(usize, bool)> = Vec::new();
        for index in 0..count {
            if index != 0 {
                let (parent, left_side) = pending.pop().ok_or("tsquery has trailing items")?;
                match &mut nodes[parent] {
                    TsQueryNode::Not(operand) => *operand = index,
                    TsQueryNode::Binary { left, right, .. } => {
                        if left_side {
                            *left = index;
                        } else {
                            *right = index;
                        }
                    }
                    TsQueryNode::Operand { .. } => return Err("tsquery operand has children".into()),
                }
            }
            let node = match reader.u8()? {
                Self::ITEM_OPERAND => {
                    let weight = reader.u8()?;
                    let prefix = reader.u8()? != 0;
                    let lexeme = reader.c_string()?.to_string();
                    TsQueryNode::Operand { lexeme, weight, prefix }
                }
                Self::ITEM_OPERATOR => {
                    let operator = reader.u8()?;
                    match operator {
                        Self::OPERATOR_NOT => {
                            pending.push((index, false));
                            TsQueryNode::Not(0)
                        }
                        Self::OPERATOR_AND | Self::OPERATOR_OR | Self::OPERATOR_PHRASE => {
                            let distance = if operator == Self::OPERATOR_PHRASE { reader.i16()? } else { 0 };
                            // Wire prefix order visits the right operand first.
                            pending.push((index, true));
                            pending.push((index, false));
                            TsQueryNode::Binary { operator, distance, left: 0, right: 0 }
                        }
                        _ => return Err(format!("invalid tsquery operator {operator}").into()),
                    }
                }
                item => return Err(format!("invalid tsquery item type {item}").into()),
            };
            nodes.push(node);
        }
        if !pending.is_empty() {
            return Err("tsquery operator is missing an operand".into());
        }
        Ok(nodes)
    }

    fn priority(operator: u8) -> i32 {
        match operator {
            Self::OPERATOR_NOT => 4,
            Self::OPERATOR_PHRASE => 3,
            Self::OPERATOR_AND => 2,
            _ => 1,
        }
    }

    fn render(nodes: &[TsQueryNode]) -> String {
        let mut out = String::new();
        if nodes.is_empty() {
            return out;
        }
        let mut pending = vec![RenderStep::Node { index: 0, parent_priority: -1, right_phrase: false }];
        while let Some(step) = pending.pop() {
            match step {
                RenderStep::Text(text) => out.push_str(text),
                RenderStep::Operator { operator, distance } => match operator {
                    Self::OPERATOR_OR => out.push_str(" | "),
                    Self::OPERATOR_AND => out.push_str(" & "),
                    _ if distance == 1 => out.push_str(" <-> "),
                    _ => write!(out, " <{distance}> ").expect("writing into a String never fails"),
                },
                RenderStep::Node { index, parent_priority, right_phrase } => match &nodes[index] {
                    TsQueryNode::Operand { lexeme, weight, prefix } => {
                        out.push('\'');
                        for character in lexeme.chars() {
                            if character == '\'' || character == '\\' {
                                out.push(character);
                            }
                            out.push(character);
                        }
                        out.push('\'');
                        if *weight != 0 || *prefix {
                            out.push(':');
                            if *prefix {
                                out.push('*');
                            }
                            for (bit, letter) in [(3, 'A'), (2, 'B'), (1, 'C'), (0, 'D')] {
                                if weight & (1 << bit) != 0 {
                                    out.push(letter);
                                }
                            }
                        }
                    }
                    TsQueryNode::Not(operand) => {
                        let priority = Self::priority(Self::OPERATOR_NOT);
                        let parentheses = priority < parent_priority;
                        if parentheses {
                            out.push_str("( ");
                            pending.push(RenderStep::Text(" )"));
                        }
                        out.push('!');
                        pending.push(RenderStep::Node {
                            index: *operand,
                            parent_priority: priority,
                            right_phrase: false,
                        });
                    }
                    TsQueryNode::Binary { operator, distance, left, right } => {
                        let priority = Self::priority(*operator);
                        let phrase = *operator == Self::OPERATOR_PHRASE;
                        if priority < parent_priority || (phrase && right_phrase) {
                            out.push_str("( ");
                            pending.push(RenderStep::Text(" )"));
                        }
                        pending.push(RenderStep::Node {
                            index: *right,
                            parent_priority: priority,
                            right_phrase: phrase,
                        });
                        pending.push(RenderStep::Operator { operator: *operator, distance: *distance });
                        pending.push(RenderStep::Node { index: *left, parent_priority: priority, right_phrase: false });
                    }
                },
            }
        }
        out
    }
}

impl PgTsQueryText {
    pub(crate) fn decode(raw: &[u8], context: &mut PgDecodeContext) -> Result<Self, BoxError> {
        let mut reader = PgWireReader::new(raw, "tsquery");
        let count = reader.count(2)?;
        context.reserve_values(count)?;
        let nodes = Self::read_nodes(&mut reader, count)?;
        reader.finish()?;
        Ok(Self(Self::render(&nodes)))
    }
}

impl<'a> FromSql<'a> for PgTsQueryText {
    fn from_sql(_ty: &Type, raw: &'a [u8]) -> Result<Self, BoxError> {
        Self::decode(raw, &mut PgDecodeContext::new(raw.len()))
    }

    fn accepts(postgres_type: &Type) -> bool {
        *postgres_type == Type::TSQUERY
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    use tokio_postgres::types::{FromSql, Type};

    #[test]
    fn deep_negation_reads_renders_and_drops_without_recursion() {
        let depth = 20_000_i32;
        let mut raw = (depth + 1).to_be_bytes().to_vec();
        for _ in 0..depth {
            raw.extend_from_slice(&[2, 1]);
        }
        tsquery_operand(&mut raw, "cat", 0, false);
        let text = PgTsQueryText::from_sql(&Type::TSQUERY, &raw).unwrap().0;
        assert_eq!(text, format!("{}'cat'", "!".repeat(depth as usize)));
    }

    #[test]
    fn empty_query_rejects_trailing_bytes() {
        assert!(PgTsQueryText::from_sql(&Type::TSQUERY, &[0, 0, 0, 0, 1]).is_err());
    }

    fn tsquery_operand(out: &mut Vec<u8>, lexeme: &str, weight: u8, prefix: bool) {
        out.push(1);
        out.push(weight);
        out.push(u8::from(prefix));
        out.extend_from_slice(lexeme.as_bytes());
        out.push(0);
    }

    #[test]
    fn tsquery_decodes_real_captured_wire_bytes_matching_asyncpgs_text_output() {
        // plainto_tsquery('english', 'fat cats') as rust_pg received it.
        let raw = b"\x00\x00\x00\x03\x02\x02\x01\x00\x00cat\x00\x01\x00\x00fat\x00";
        assert_eq!(PgTsQueryText::from_sql(&Type::TSQUERY, raw).expect("must decode").0, "'fat' & 'cat'");
    }

    #[test]
    fn tsquery_renders_parentheses_negation_weights_prefix_and_phrase_distance() {
        // 'fat' & ( 'cat':*AB | !'dog' ) - prefix order, right operand first.
        let mut raw = 6i32.to_be_bytes().to_vec();
        raw.extend_from_slice(&[2, 2]);
        raw.extend_from_slice(&[2, 3]);
        raw.extend_from_slice(&[2, 1]);
        tsquery_operand(&mut raw, "dog", 0, false);
        tsquery_operand(&mut raw, "cat", 0b1100, true);
        tsquery_operand(&mut raw, "fat", 0, false);
        assert_eq!(
            PgTsQueryText::from_sql(&Type::TSQUERY, &raw).expect("must decode").0,
            "'fat' & ( 'cat':*AB | !'dog' )"
        );

        // A phrase operator as the right operand of another one keeps its parentheses.
        let mut raw = 5i32.to_be_bytes().to_vec();
        raw.extend_from_slice(&[2, 4, 0, 1]);
        raw.extend_from_slice(&[2, 4, 0, 1]);
        tsquery_operand(&mut raw, "c", 0, false);
        tsquery_operand(&mut raw, "b", 0, false);
        tsquery_operand(&mut raw, "a", 0, false);
        assert_eq!(PgTsQueryText::from_sql(&Type::TSQUERY, &raw).expect("must decode").0, "'a' <-> ( 'b' <-> 'c' )");

        let mut raw = 3i32.to_be_bytes().to_vec();
        raw.extend_from_slice(&[2, 4, 0, 2]);
        tsquery_operand(&mut raw, "b", 0, false);
        tsquery_operand(&mut raw, "a", 0, false);
        assert_eq!(PgTsQueryText::from_sql(&Type::TSQUERY, &raw).expect("must decode").0, "'a' <2> 'b'");
    }

    #[test]
    fn tsquery_escapes_quote_and_backslash_and_decodes_an_empty_query() {
        let mut raw = 1i32.to_be_bytes().to_vec();
        tsquery_operand(&mut raw, "it's\\", 0, false);
        assert_eq!(PgTsQueryText::from_sql(&Type::TSQUERY, &raw).expect("must decode").0, "'it''s\\\\'");
        assert_eq!(PgTsQueryText::from_sql(&Type::TSQUERY, &0i32.to_be_bytes()).expect("must decode").0, "");
    }

    #[test]
    fn tsquery_rejects_malformed_payloads() {
        // An AND operator with only one operand.
        let mut raw = 2i32.to_be_bytes().to_vec();
        raw.extend_from_slice(&[2, 2]);
        tsquery_operand(&mut raw, "a", 0, false);
        assert!(PgTsQueryText::from_sql(&Type::TSQUERY, &raw).is_err());
        // An unterminated lexeme.
        let mut raw = 1i32.to_be_bytes().to_vec();
        raw.extend_from_slice(&[1, 0, 0]);
        raw.extend_from_slice(b"abc");
        assert!(PgTsQueryText::from_sql(&Type::TSQUERY, &raw).is_err());
        assert!(PgTsQueryText::from_sql(&Type::TSQUERY, &[0, 0]).is_err());
    }
}
