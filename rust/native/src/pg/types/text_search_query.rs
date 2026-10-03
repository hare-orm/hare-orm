//! `TSQUERY`, read as the text `tsqueryout` prints.

use std::fmt::Write as _;

use bytes::Buf;
use tokio_postgres::types::{FromSql, Type};

use crate::pg::value::BoxError;

/// One node of a decoded `TSQUERY`.
pub(crate) enum TsQueryNode {
    Operand { lexeme: String, weight: u8, prefix: bool },
    Not(Box<TsQueryNode>),
    Binary { operator: u8, distance: i16, left: Box<TsQueryNode>, right: Box<TsQueryNode> },
}

/// `TSQUERY` rendered as the text `tsqueryout` itself prints (and asyncpg, which reads a tsquery
/// as that text, hands back), e.g. `'fat' & ( 'cat' | !'dog' )`.
///
/// Its wire format (`tsquerysend`) is a 4-byte item count, then the items in prefix order: an
/// operand is a type byte (1), a weight bitmask byte, a prefix-flag byte and a null-terminated
/// lexeme; an operator is a type byte (2) and an operator byte (1 NOT, 2 AND, 3 OR, 4 PHRASE,
/// the last followed by a 2-byte distance). A binary operator's right operand comes first, then
/// its left one.
pub(crate) struct PgTsQueryText(pub(crate) String);

impl PgTsQueryText {
    pub(crate) const ITEM_OPERAND: u8 = 1;
    pub(crate) const ITEM_OPERATOR: u8 = 2;
    pub(crate) const OPERATOR_NOT: u8 = 1;
    pub(crate) const OPERATOR_AND: u8 = 2;
    pub(crate) const OPERATOR_OR: u8 = 3;
    pub(crate) const OPERATOR_PHRASE: u8 = 4;
    /// The deepest operator nesting decoded - deeper is an error rather than a stack overflow.
    pub(crate) const MAX_DEPTH: usize = 4096;

    pub(crate) fn read_node(raw: &mut &[u8], remaining_items: &mut i32, depth: usize) -> Result<TsQueryNode, BoxError> {
        if depth > Self::MAX_DEPTH {
            return Err(format!("tsquery nests operators deeper than {} levels", Self::MAX_DEPTH).into());
        }
        if *remaining_items <= 0 {
            return Err("invalid tsquery payload: operator is missing an operand".into());
        }
        *remaining_items -= 1;
        let item_type = raw.try_get_u8().map_err(|_| "invalid tsquery payload: truncated item")?;
        match item_type {
            Self::ITEM_OPERAND => {
                let weight = raw.try_get_u8().map_err(|_| "invalid tsquery payload: truncated operand")?;
                let prefix = raw.try_get_u8().map_err(|_| "invalid tsquery payload: truncated operand")? != 0;
                let null_position =
                    raw.iter().position(|&byte| byte == 0).ok_or("invalid tsquery payload: unterminated lexeme")?;
                let lexeme = std::str::from_utf8(&raw[..null_position])
                    .map_err(|e| format!("invalid tsquery payload: lexeme is not valid UTF-8: {e}"))?
                    .to_string();
                raw.advance(null_position + 1);
                Ok(TsQueryNode::Operand { lexeme, weight, prefix })
            }
            Self::ITEM_OPERATOR => {
                let operator = raw.try_get_u8().map_err(|_| "invalid tsquery payload: truncated operator")?;
                match operator {
                    Self::OPERATOR_NOT => {
                        Ok(TsQueryNode::Not(Box::new(Self::read_node(raw, remaining_items, depth + 1)?)))
                    }
                    Self::OPERATOR_AND | Self::OPERATOR_OR | Self::OPERATOR_PHRASE => {
                        let distance = if operator == Self::OPERATOR_PHRASE {
                            raw.try_get_i16().map_err(|_| "invalid tsquery payload: truncated phrase distance")?
                        } else {
                            0
                        };
                        let right = Self::read_node(raw, remaining_items, depth + 1)?;
                        let left = Self::read_node(raw, remaining_items, depth + 1)?;
                        Ok(TsQueryNode::Binary { operator, distance, left: Box::new(left), right: Box::new(right) })
                    }
                    _ => Err(format!("invalid tsquery payload: unknown operator {operator}").into()),
                }
            }
            _ => Err(format!("invalid tsquery payload: unknown item type {item_type}").into()),
        }
    }

    /// An operator's binding priority, as `tsqueryout` uses it to decide on parentheses.
    pub(crate) fn priority(operator: u8) -> i32 {
        match operator {
            Self::OPERATOR_NOT => 4,
            Self::OPERATOR_PHRASE => 3,
            Self::OPERATOR_AND => 2,
            _ => 1,
        }
    }

    pub(crate) fn render(node: &TsQueryNode, parent_priority: i32, is_right_phrase_operand: bool, out: &mut String) {
        match node {
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
                let needs_parentheses = priority < parent_priority;
                if needs_parentheses {
                    out.push_str("( ");
                }
                out.push('!');
                Self::render(operand, priority, false, out);
                if needs_parentheses {
                    out.push_str(" )");
                }
            }
            TsQueryNode::Binary { operator, distance, left, right } => {
                let priority = Self::priority(*operator);
                let is_phrase = *operator == Self::OPERATOR_PHRASE;
                let needs_parentheses = priority < parent_priority || (is_phrase && is_right_phrase_operand);
                if needs_parentheses {
                    out.push_str("( ");
                }
                Self::render(left, priority, false, out);
                match *operator {
                    Self::OPERATOR_OR => out.push_str(" | "),
                    Self::OPERATOR_AND => out.push_str(" & "),
                    _ if *distance != 1 => write!(out, " <{distance}> ").expect("writing into a String never fails"),
                    _ => out.push_str(" <-> "),
                }
                Self::render(right, priority, is_phrase, out);
                if needs_parentheses {
                    out.push_str(" )");
                }
            }
        }
    }
}

impl<'a> FromSql<'a> for PgTsQueryText {
    fn from_sql(_ty: &Type, mut raw: &'a [u8]) -> Result<Self, BoxError> {
        let mut remaining_items = raw.try_get_i32().map_err(|_| "invalid tsquery payload: too short")?;
        if remaining_items < 0 {
            return Err(format!("invalid tsquery payload: negative item count {remaining_items}").into());
        }
        if remaining_items == 0 {
            return Ok(PgTsQueryText(String::new()));
        }
        let root = Self::read_node(&mut raw, &mut remaining_items, 0)?;
        if remaining_items != 0 || !raw.is_empty() {
            return Err("invalid tsquery payload: trailing items after the query".into());
        }
        let mut text = String::new();
        Self::render(&root, -1, false, &mut text);
        Ok(PgTsQueryText(text))
    }

    fn accepts(ty: &Type) -> bool {
        *ty == Type::TSQUERY
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    use tokio_postgres::types::{FromSql, Type};

    fn tsquery_operand(out: &mut Vec<u8>, lexeme: &str, weight: u8, prefix: bool) {
        out.push(1);
        out.push(weight);
        out.push(u8::from(prefix));
        out.extend_from_slice(lexeme.as_bytes());
        out.push(0);
    }

    #[test]
    fn tsquery_decodes_real_captured_wire_bytes_matching_asyncpgs_own_text_output() {
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
