//! A stored date or datetime text read into its fields - the forms hare writes; any other goes to
//! the Python function that reads it.

use std::fmt::Write;

use chrono::{Datelike, NaiveDate, NaiveDateTime, NaiveTime, Timelike};

/// A date or datetime, with the UTC offset an aware one was written with.
pub struct IsoMoment {
    pub moment: NaiveDateTime,
    /// The offset in seconds; None for a naive value.
    pub offset_seconds: Option<i32>,
    /// The text held only a date.
    pub is_date: bool,
}

/// The number of the ASCII digits at `start..start + length`.
fn read_number(bytes: &[u8], start: usize, length: usize) -> Option<u32> {
    let digits = bytes.get(start..start + length)?;
    if !digits.iter().all(u8::is_ascii_digit) {
        return None;
    }
    Some(digits.iter().fold(0, |number, digit| number * 10 + u32::from(digit - b'0')))
}

impl IsoMoment {
    /// The moment of a text in the form `YYYY-MM-DD[( |T)HH:MM[:SS[.f{1,6}]][(+|-)HH:MM]]`; None for
    /// any other text.
    pub fn parse(text: &str) -> Option<Self> {
        let bytes = text.as_bytes();
        if bytes.get(4) != Some(&b'-') || bytes.get(7) != Some(&b'-') {
            return None;
        }
        let date = NaiveDate::from_ymd_opt(
            i32::try_from(read_number(bytes, 0, 4)?).ok()?,
            read_number(bytes, 5, 2)?,
            read_number(bytes, 8, 2)?,
        )?;
        if date.year() < 1 {
            return None;
        }
        if bytes.len() == 10 {
            return Some(IsoMoment { moment: date.and_time(NaiveTime::MIN), offset_seconds: None, is_date: true });
        }
        if !matches!(bytes.get(10), Some(b' ' | b'T')) || bytes.get(13) != Some(&b':') {
            return None;
        }
        let hour = read_number(bytes, 11, 2)?;
        let minute = read_number(bytes, 14, 2)?;
        let mut position = 16;
        let mut second = 0;
        let mut microsecond = 0;
        if bytes.get(position) == Some(&b':') {
            second = read_number(bytes, position + 1, 2)?;
            position += 3;
            if bytes.get(position) == Some(&b'.') {
                let start = position + 1;
                let mut end = start;
                while bytes.get(end).is_some_and(u8::is_ascii_digit) {
                    end += 1;
                }
                let length = end - start;
                if !(1..=6).contains(&length) {
                    return None;
                }
                microsecond = read_number(bytes, start, length)? * 10_u32.pow((6 - length) as u32);
                position = end;
            }
        }
        let time = NaiveTime::from_hms_micro_opt(hour, minute, second, microsecond)?;
        let offset_seconds = match bytes.get(position) {
            None => None,
            Some(&sign @ (b'+' | b'-')) => {
                if bytes.len() != position + 6 || bytes.get(position + 3) != Some(&b':') {
                    return None;
                }
                let offset_hour = read_number(bytes, position + 1, 2)?;
                let offset_minute = read_number(bytes, position + 4, 2)?;
                if offset_hour >= 24 || offset_minute >= 60 {
                    return None;
                }
                let seconds = i32::try_from(offset_hour * 3600 + offset_minute * 60).ok()?;
                Some(if sign == b'-' { -seconds } else { seconds })
            }
            Some(_) => return None,
        };
        Some(IsoMoment { moment: date.and_time(time), offset_seconds, is_date: false })
    }
}

/// `date.isoformat()`.
pub fn format_date(date: NaiveDate) -> String {
    format!("{:04}-{:02}-{:02}", date.year(), date.month(), date.day())
}

/// `time.isoformat()` of a naive time - microseconds only when there are any.
pub fn format_time(time: NaiveTime) -> String {
    let microsecond = time.nanosecond() / 1000;
    if microsecond == 0 {
        format!("{:02}:{:02}:{:02}", time.hour(), time.minute(), time.second())
    } else {
        format!("{:02}:{:02}:{:02}.{microsecond:06}", time.hour(), time.minute(), time.second())
    }
}

/// `datetime.isoformat(" ")`, with the UTC offset of an aware value.
pub fn format_moment(moment: NaiveDateTime, offset_seconds: Option<i32>) -> String {
    let mut text = format!("{} {}", format_date(moment.date()), format_time(moment.time()));
    if let Some(offset) = offset_seconds {
        let sign = if offset < 0 { '-' } else { '+' };
        let magnitude = offset.unsigned_abs();
        let _ = write!(text, "{sign}{:02}:{:02}", magnitude / 3600, magnitude % 3600 / 60);
    }
    text
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_the_stored_forms() {
        let moment = IsoMoment::parse("2024-03-01 10:30:05.25+03:00").expect("a datetime");
        assert_eq!(format_moment(moment.moment, moment.offset_seconds), "2024-03-01 10:30:05.250000+03:00");
        assert!(IsoMoment::parse("2024-03-01").expect("a date").is_date);
        assert_eq!(IsoMoment::parse("2024-03-01T10:30").expect("a datetime").offset_seconds, None);
        for text in
            ["2024-3-01", "2024-02-30", "2024-03-01 10", "2024-03-01 10:30Z", "2024-03-01 10:30:00+0300", "0000-01-01"]
        {
            assert!(IsoMoment::parse(text).is_none(), "{text}");
        }
    }
}
