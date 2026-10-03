//! `TSVECTOR`, read as the text `tsvector_out` prints.

use bytes::Buf;
use tokio_postgres::types::{FromSql, Type};

use crate::pg::value::BoxError;

/// `TSVECTOR`'s wire format (`tsvectorsend`): a 4-byte lexeme count, then per lexeme a
/// null-terminated UTF-8 string, a 2-byte position count, and that many 2-byte position+weight
/// entries (top 2 bits = weight, bottom 14 bits = 1-based position within the indexed document).
/// Weight 3/2/1 render as the 'A'/'B'/'C' suffix `tsvector_out` itself uses; weight 0 ('D',
/// Postgres's own "no explicit weight" default) renders with no suffix at all - confirmed
/// against a real connection (`SELECT setweight(to_tsvector('cat'), 'D')::text` prints `'cat':1`,
/// with no letter). A lexeme containing `'` or `\` is escaped by doubling it, matching
/// `tsvector_out`'s own quoting (confirmed the same way). Without a dedicated decoder here, this
/// column fell through to the generic "unknown type" fallback (`AnyAsText`), which decides
/// text-vs-hex by checking whether the raw wire bytes happen to be valid UTF-8 - `tsvector`'s
/// real binary wire format is essentially never valid UTF-8 (the leading 4-byte lexeme count
/// alone all but guarantees a non-UTF-8 byte), so every tsvector value was silently hex-encoded
/// instead of decoded, on every read, not just a cache-hit - confirmed live against real
/// Postgres. hare has no dedicated full-text-search field type, so this is for raw-SQL/inspectdb
/// interop and any annotation/Case/Coalesce expression touching a tsvector column.
pub(crate) struct PgTsVectorText(pub(crate) String);

impl<'a> FromSql<'a> for PgTsVectorText {
    fn from_sql(_ty: &Type, mut raw: &'a [u8]) -> Result<Self, BoxError> {
        if raw.len() < 4 {
            return Err("invalid tsvector payload: too short".into());
        }
        let nitems = raw.get_i32();
        if nitems < 0 {
            return Err(format!("invalid tsvector payload: negative lexeme count {nitems}").into());
        }
        let mut rendered_entries = Vec::with_capacity(nitems as usize);
        for _ in 0..nitems {
            let null_pos = raw.iter().position(|&b| b == 0).ok_or("invalid tsvector payload: unterminated lexeme")?;
            let lexeme = std::str::from_utf8(&raw[..null_pos])
                .map_err(|e| format!("invalid tsvector payload: lexeme is not valid UTF-8: {e}"))?
                .replace('\\', "\\\\")
                .replace('\'', "''");
            raw.advance(null_pos + 1);
            if raw.len() < 2 {
                return Err("invalid tsvector payload: truncated position count".into());
            }
            let npos = raw.get_i16();
            if npos < 0 {
                return Err(format!("invalid tsvector payload: negative position count {npos}").into());
            }
            let npos = npos as usize;
            if raw.len() < npos * 2 {
                return Err("invalid tsvector payload: truncated position list".into());
            }
            let mut positions = Vec::with_capacity(npos);
            for _ in 0..npos {
                let raw_pos = raw.get_u16();
                let position = raw_pos & 0x3FFF;
                let weight_letter = match raw_pos >> 14 {
                    3 => "A",
                    2 => "B",
                    1 => "C",
                    _ => "",
                };
                positions.push(format!("{position}{weight_letter}"));
            }
            rendered_entries.push(if positions.is_empty() {
                format!("'{lexeme}'")
            } else {
                format!("'{lexeme}':{}", positions.join(","))
            });
        }
        Ok(PgTsVectorText(rendered_entries.join(" ")))
    }

    fn accepts(ty: &Type) -> bool {
        *ty == Type::TS_VECTOR
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
    fn tsvector_decodes_real_captured_wire_bytes_matching_asyncpgs_own_text_output() {
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
