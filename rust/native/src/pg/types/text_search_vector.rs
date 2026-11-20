//! `TSVECTOR`, read as the text `tsvector_out` prints.

use std::fmt::Write as _;

use tokio_postgres::types::{FromSql, Type};

use crate::pg::decode_context::PgDecodeContext;
use crate::pg::value::BoxError;
use crate::pg::wire_reader::PgWireReader;

/// `TSVECTOR`'s wire format (`tsvectorsend`): a 4-byte lexeme count, then per lexeme a
/// null-terminated UTF-8 string, a 2-byte position count, and that many 2-byte position+weight
/// entries (top 2 bits = weight, bottom 14 bits = 1-based position within the indexed document).
/// Weight 3/2/1 render as the 'A'/'B'/'C' suffix `tsvector_out` itself uses; weight 0 ('D',
/// Postgres's own "no explicit weight" default) renders with no suffix at all, as
/// `SELECT setweight(to_tsvector('cat'), 'D')::text` prints `'cat':1`. A lexeme containing `'` or `\`
/// is escaped by doubling it, matching `tsvector_out`'s own quoting. The value is read as the text
/// PostgreSQL prints for it - a `TSVectorField`, raw SQL and any expression reading a tsvector
/// column alike.
pub(crate) struct PgTsVectorText(pub(crate) String);

impl PgTsVectorText {
    pub(crate) fn decode(raw: &[u8], context: &mut PgDecodeContext) -> Result<Self, BoxError> {
        let mut reader = PgWireReader::new(raw, "tsvector");
        let nitems = reader.count(3)?;
        context.reserve_values(nitems)?;
        let mut text = String::new();
        for index in 0..nitems {
            let lexeme = reader.c_string()?.replace('\\', "\\\\").replace('\'', "''");
            let position_count = reader.i16()?;
            let npos = reader.checked_count(i32::from(position_count), 2)?;
            context.reserve_values(npos)?;
            if index != 0 {
                text.push(' ');
            }
            write!(text, "'{lexeme}'").expect("writing into a String never fails");
            if npos != 0 {
                text.push(':');
            }
            for position_index in 0..npos {
                let raw_pos = reader.u16()?;
                let position = raw_pos & 0x3FFF;
                let weight_letter = match raw_pos >> 14 {
                    3 => "A",
                    2 => "B",
                    1 => "C",
                    _ => "",
                };
                if position_index != 0 {
                    text.push(',');
                }
                write!(text, "{position}{weight_letter}").expect("writing into a String never fails");
            }
        }
        reader.finish()?;
        Ok(PgTsVectorText(text))
    }
}

impl<'a> FromSql<'a> for PgTsVectorText {
    fn from_sql(_ty: &Type, raw: &'a [u8]) -> Result<Self, BoxError> {
        Self::decode(raw, &mut PgDecodeContext::new(raw.len()))
    }

    fn accepts(postgres_type: &Type) -> bool {
        *postgres_type == Type::TS_VECTOR
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    use tokio_postgres::types::{FromSql, Type};

    /// Real wire bytes captured from a live Postgres connection for `setweight(to_tsvector(
    /// 'cat sat'), 'A') || setweight(to_tsvector('on the mat'), 'B')` - 3 lexemes ("on"/"the"
    /// are English stopwords, dropped by `to_tsvector`), confirmed byte-for-byte against
    /// asyncpg's own text-format decode of the identical value ("'cat':1A 'mat':5B 'sat':2A").
    #[test]
    fn tsvector_decodes_real_captured_wire_bytes_matching_asyncpgs_text_output() {
        let mut raw = Vec::new();
        raw.extend_from_slice(&3i32.to_be_bytes()); // nitems
        raw.extend_from_slice(b"cat\0");
        raw.extend_from_slice(&1i16.to_be_bytes());
        raw.extend_from_slice(&0xc001u16.to_be_bytes()); // weight 3 ('A'), position 1
        raw.extend_from_slice(b"mat\0");
        raw.extend_from_slice(&1i16.to_be_bytes());
        raw.extend_from_slice(&0x8005u16.to_be_bytes()); // weight 2 ('B'), position 5
        raw.extend_from_slice(b"sat\0");
        raw.extend_from_slice(&1i16.to_be_bytes());
        raw.extend_from_slice(&0xc002u16.to_be_bytes()); // weight 3 ('A'), position 2
        let value = PgTsVectorText::from_sql(&Type::TS_VECTOR, &raw).expect("well-formed tsvector payload must decode");
        assert_eq!(value.0, "'cat':1A 'mat':5B 'sat':2A");
    }

    #[test]
    fn tsvector_weight_zero_renders_with_no_letter_suffix() {
        // nitems=1, lexeme "cat"+NUL, npos=1, one position entry: weight 0 ('D'), position 1.
        let mut raw = Vec::new();
        raw.extend_from_slice(&1i32.to_be_bytes());
        raw.extend_from_slice(b"cat\0");
        raw.extend_from_slice(&1i16.to_be_bytes());
        raw.extend_from_slice(&1u16.to_be_bytes()); // weight bits 00, position 1
        let value = PgTsVectorText::from_sql(&Type::TS_VECTOR, &raw).expect("well-formed tsvector payload must decode");
        assert_eq!(value.0, "'cat':1");
    }

    #[test]
    fn tsvector_lexeme_with_no_positions_renders_with_no_colon_suffix() {
        // array_to_tsvector(ARRAY['cat']) - a lexeme with npos=0 has no position list at all.
        let mut raw = Vec::new();
        raw.extend_from_slice(&1i32.to_be_bytes());
        raw.extend_from_slice(b"cat\0");
        raw.extend_from_slice(&0i16.to_be_bytes());
        let value = PgTsVectorText::from_sql(&Type::TS_VECTOR, &raw).expect("well-formed tsvector payload must decode");
        assert_eq!(value.0, "'cat'");
    }

    #[test]
    fn tsvector_multiple_positions_on_one_lexeme_are_comma_joined() {
        // array_to_tsvector-style single lexeme "word" at positions 1 and 5, both weight D.
        let mut raw = Vec::new();
        raw.extend_from_slice(&1i32.to_be_bytes());
        raw.extend_from_slice(b"word\0");
        raw.extend_from_slice(&2i16.to_be_bytes());
        raw.extend_from_slice(&1u16.to_be_bytes());
        raw.extend_from_slice(&5u16.to_be_bytes());
        let value = PgTsVectorText::from_sql(&Type::TS_VECTOR, &raw).expect("well-formed tsvector payload must decode");
        assert_eq!(value.0, "'word':1,5");
    }

    #[test]
    fn tsvector_escapes_single_quote_and_backslash_in_a_lexeme() {
        // A literal lexeme (as array_to_tsvector accepts verbatim, no tokenization) containing
        // both a single quote and a backslash - confirmed live that Postgres's own tsvector_out
        // doubles both characters (`it's` -> `it''s`, `back\slash` -> `back\\slash`).
        let mut raw = Vec::new();
        raw.extend_from_slice(&1i32.to_be_bytes());
        raw.extend_from_slice(b"it's\\\0");
        raw.extend_from_slice(&0i16.to_be_bytes());
        let value = PgTsVectorText::from_sql(&Type::TS_VECTOR, &raw).expect("well-formed tsvector payload must decode");
        assert_eq!(value.0, "'it''s\\\\'");
    }

    #[test]
    fn tsvector_rejects_unterminated_lexeme() {
        let mut raw = Vec::new();
        raw.extend_from_slice(&1i32.to_be_bytes());
        raw.extend_from_slice(b"cat"); // no NUL terminator
        assert!(PgTsVectorText::from_sql(&Type::TS_VECTOR, &raw).is_err());
    }
}
