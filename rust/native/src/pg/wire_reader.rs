//! Checked consumption of one PostgreSQL binary value.
use crate::pg::value::BoxError;
use bytes::Buf;

pub(crate) struct PgWireReader<'a> {
    remaining: &'a [u8],
    label: &'static str,
}

impl<'a> PgWireReader<'a> {
    pub(crate) fn new(raw: &'a [u8], label: &'static str) -> Self {
        Self { remaining: raw, label }
    }

    pub(crate) fn remaining(&self) -> &'a [u8] {
        self.remaining
    }

    pub(crate) fn take(&mut self, length: usize) -> Result<&'a [u8], BoxError> {
        if length > self.remaining.len() {
            return Err(format!("invalid {} payload: truncated value of {length} bytes", self.label).into());
        }
        let (value, rest) = self.remaining.split_at(length);
        self.remaining = rest;
        Ok(value)
    }

    pub(crate) fn u8(&mut self) -> Result<u8, BoxError> {
        Ok(self.remaining.try_get_u8()?)
    }

    pub(crate) fn i16(&mut self) -> Result<i16, BoxError> {
        Ok(self.remaining.try_get_i16()?)
    }

    pub(crate) fn u16(&mut self) -> Result<u16, BoxError> {
        Ok(self.remaining.try_get_u16()?)
    }

    pub(crate) fn i32(&mut self) -> Result<i32, BoxError> {
        Ok(self.remaining.try_get_i32()?)
    }

    pub(crate) fn u32(&mut self) -> Result<u32, BoxError> {
        Ok(self.remaining.try_get_u32()?)
    }

    pub(crate) fn i64(&mut self) -> Result<i64, BoxError> {
        Ok(self.remaining.try_get_i64()?)
    }

    pub(crate) fn count(&mut self, minimum_item_bytes: usize) -> Result<usize, BoxError> {
        let count = self.i32()?;
        self.checked_count(count, minimum_item_bytes)
    }

    pub(crate) fn checked_count(&self, count: i32, minimum_item_bytes: usize) -> Result<usize, BoxError> {
        let count = usize::try_from(count)
            .map_err(|_| format!("invalid {} payload: negative item count {count}", self.label))?;
        let minimum_size = count.checked_mul(minimum_item_bytes).ok_or("item byte count overflow")?;
        if minimum_size > self.remaining.len() {
            return Err(format!("invalid {} payload: item count exceeds remaining bytes", self.label).into());
        }
        Ok(count)
    }

    pub(crate) fn nullable_bytes(&mut self) -> Result<Option<&'a [u8]>, BoxError> {
        let length = self.i32()?;
        if length == -1 {
            return Ok(None);
        }
        let length = usize::try_from(length)
            .map_err(|_| format!("invalid {} payload: negative value length {length}", self.label))?;
        self.take(length).map(Some)
    }

    pub(crate) fn c_string(&mut self) -> Result<&'a str, BoxError> {
        let length = self
            .remaining
            .iter()
            .position(|byte| *byte == 0)
            .ok_or_else(|| format!("invalid {} payload: unterminated string", self.label))?;
        let value = self.take(length)?;
        self.take(1)?;
        Ok(std::str::from_utf8(value)?)
    }

    pub(crate) fn finish(self) -> Result<(), BoxError> {
        if self.remaining.is_empty() {
            Ok(())
        } else {
            Err(format!("invalid {} payload: trailing bytes", self.label).into())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn length_prefix_accepts_only_null_or_present_bytes() {
        assert!(PgWireReader::new(&(-1_i32).to_be_bytes(), "value").nullable_bytes().unwrap().is_none());
        assert!(PgWireReader::new(&(-2_i32).to_be_bytes(), "value").nullable_bytes().is_err());
        assert!(PgWireReader::new(&5_i32.to_be_bytes(), "value").nullable_bytes().is_err());
        let raw = [0, 0, 0, 2, 7, 8];
        let mut reader = PgWireReader::new(&raw, "value");
        assert_eq!(reader.nullable_bytes().unwrap(), Some(&[7, 8][..]));
        reader.finish().unwrap();
    }

    #[test]
    fn counts_are_bounded_by_wire_bytes_and_values_must_finish() {
        assert!(PgWireReader::new(&i32::MAX.to_be_bytes(), "value").count(4).is_err());
        assert!(PgWireReader::new(&[1], "value").finish().is_err());
        for length in 0..4 {
            assert!(PgWireReader::new(&[0, 0, 0, 0][..length], "value").i32().is_err());
        }
    }
}
