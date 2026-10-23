//! ISO 8601 text of dates, times and datetimes in the forms SQLite stores them - `2024-06-01`,
//! `12:30:45.000250+03:00`, `2024-06-01 08:00:00+00:00`. Any other spelling is left to the Python
//! parser: these parse only what they can read exactly.

use std::fmt::Write;

/// A calendar date.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct IsoDate {
    pub year: i32,
    pub month: u8,
    pub day: u8,
}

/// A time of day with an optional UTC offset in seconds.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct IsoTime {
    pub hour: u8,
    pub minute: u8,
    pub second: u8,
    pub microsecond: u32,
    pub offset_seconds: Option<i32>,
}

/// A date and a time of day.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct IsoDateTime {
    pub date: IsoDate,
    pub time: IsoTime,
}

fn digits(text: &[u8], count: usize) -> Option<u32> {
    if text.len() < count {
        return None;
    }
    let mut value = 0u32;
    for byte in &text[..count] {
        if !byte.is_ascii_digit() {
            return None;
        }
        value = value * 10 + u32::from(byte - b'0');
    }
    Some(value)
}

fn days_in_month(year: i32, month: u8) -> u8 {
    match month {
        4 | 6 | 9 | 11 => 30,
        2 if (year % 4 == 0 && year % 100 != 0) || year % 400 == 0 => 29,
        2 => 28,
        _ => 31,
    }
}

/// `YYYY-MM-DD` - and nothing after it.
pub fn parse_date(text: &str) -> Option<IsoDate> {
    let bytes = text.as_bytes();
    if bytes.len() != 10 {
        return None;
    }
    parse_date_prefix(bytes)
}

fn parse_date_prefix(bytes: &[u8]) -> Option<IsoDate> {
    if bytes.len() < 10 || bytes[4] != b'-' || bytes[7] != b'-' {
        return None;
    }
    let year = i32::try_from(digits(bytes, 4)?).ok()?;
    let month = u8::try_from(digits(&bytes[5..], 2)?).ok()?;
    let day = u8::try_from(digits(&bytes[8..], 2)?).ok()?;
    if year < 1 || !(1..=12).contains(&month) || day < 1 || day > days_in_month(year, month) {
        return None;
    }
    Some(IsoDate { year, month, day })
}

/// `HH:MM:SS`, optionally `.ffffff` (exactly six digits) and `+HH:MM`/`-HH:MM` - and nothing after it.
pub fn parse_time(text: &str) -> Option<IsoTime> {
    let bytes = text.as_bytes();
    let (time, rest) = parse_time_prefix(bytes)?;
    if !rest.is_empty() {
        return None;
    }
    Some(time)
}

fn parse_time_prefix(bytes: &[u8]) -> Option<(IsoTime, &[u8])> {
    if bytes.len() < 8 || bytes[2] != b':' || bytes[5] != b':' {
        return None;
    }
    let hour = u8::try_from(digits(bytes, 2)?).ok()?;
    let minute = u8::try_from(digits(&bytes[3..], 2)?).ok()?;
    let second = u8::try_from(digits(&bytes[6..], 2)?).ok()?;
    if hour > 23 || minute > 59 || second > 59 {
        return None;
    }
    let mut rest = &bytes[8..];
    let mut microsecond = 0;
    if rest.first() == Some(&b'.') {
        microsecond = digits(&rest[1..], 6)?;
        rest = &rest[7..];
        if rest.first().is_some_and(u8::is_ascii_digit) {
            return None;
        }
    }
    let mut offset_seconds = None;
    if let Some(&sign) = rest.first() {
        if (sign != b'+' && sign != b'-') || rest.len() != 6 || rest[3] != b':' {
            return None;
        }
        let offset_hours = i32::try_from(digits(&rest[1..], 2)?).ok()?;
        let offset_minutes = i32::try_from(digits(&rest[4..], 2)?).ok()?;
        if offset_hours > 23 || offset_minutes > 59 {
            return None;
        }
        let seconds = offset_hours * 3600 + offset_minutes * 60;
        offset_seconds = Some(if sign == b'-' { -seconds } else { seconds });
        rest = &rest[6..];
    }
    Some((IsoTime { hour, minute, second, microsecond, offset_seconds }, rest))
}

/// `YYYY-MM-DD HH:MM:SS[.ffffff][+HH:MM]`, with a space or a `T` between date and time.
pub fn parse_datetime(text: &str) -> Option<IsoDateTime> {
    let bytes = text.as_bytes();
    if bytes.len() < 19 || (bytes[10] != b' ' && bytes[10] != b'T') {
        return None;
    }
    let date = parse_date_prefix(bytes)?;
    let (time, rest) = parse_time_prefix(&bytes[11..])?;
    if !rest.is_empty() {
        return None;
    }
    Some(IsoDateTime { date, time })
}

fn write_offset(offset_seconds: i32, out: &mut String) {
    let sign = if offset_seconds < 0 { '-' } else { '+' };
    let absolute = offset_seconds.unsigned_abs();
    let _ = write!(out, "{sign}{:02}:{:02}", absolute / 3600, absolute % 3600 / 60);
    if !absolute.is_multiple_of(60) {
        let _ = write!(out, ":{:02}", absolute % 60);
    }
}

/// `date.isoformat()`.
pub fn format_date(date: IsoDate, out: &mut String) {
    let _ = write!(out, "{:04}-{:02}-{:02}", date.year, date.month, date.day);
}

/// `time.isoformat()` - the fraction only when not zero.
pub fn format_time(time: IsoTime, out: &mut String) {
    let _ = write!(out, "{:02}:{:02}:{:02}", time.hour, time.minute, time.second);
    if time.microsecond != 0 {
        let _ = write!(out, ".{:06}", time.microsecond);
    }
    if let Some(offset_seconds) = time.offset_seconds {
        write_offset(offset_seconds, out);
    }
}

/// `datetime.isoformat(" ")`.
pub fn format_datetime(value: IsoDateTime) -> String {
    let mut out = String::with_capacity(32);
    format_date(value.date, &mut out);
    out.push(' ');
    format_time(value.time, &mut out);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_and_writes_the_stored_datetime_forms() {
        for text in ["2024-06-01 08:00:00+00:00", "2024-02-29 23:59:59.000001-03:30", "0001-01-01 00:00:00"] {
            let parsed = parse_datetime(text).expect(text);
            assert_eq!(format_datetime(parsed), text);
        }
        let with_t = parse_datetime("2024-06-01T08:00:00").expect("T separator");
        assert_eq!(format_datetime(with_t), "2024-06-01 08:00:00");
    }

    #[test]
    fn leaves_other_spellings_to_the_python_parser() {
        for text in [
            "2024-06-01",
            "2024-6-01 08:00:00",
            "2023-02-29 00:00:00",
            "2024-06-01 24:00:00",
            "2024-06-01 08:00:00.5",
            "2024-06-01 08:00:00.1234567",
            "2024-06-01 08:00:00Z",
            "2024-06-01 08:00:00+0300",
            "2024-06-01 08:00:00 ",
        ] {
            assert_eq!(parse_datetime(text), None, "{text}");
        }
    }

    #[test]
    fn reads_and_writes_times_and_dates() {
        for text in ["12:30:45", "12:30:45.000250", "12:30:45+03:00", "00:00:00.999999-11:30"] {
            let mut out = String::new();
            format_time(parse_time(text).expect(text), &mut out);
            assert_eq!(out, text);
        }
        assert_eq!(parse_time("12:30"), None);
        let mut out = String::new();
        format_date(parse_date("2024-12-31").expect("date"), &mut out);
        assert_eq!(out, "2024-12-31");
        assert_eq!(parse_date("2024-12-32"), None);
    }

    #[test]
    fn offsets_with_seconds_keep_them() {
        let mut out = String::new();
        format_time(
            IsoTime { hour: 1, minute: 2, second: 3, microsecond: 0, offset_seconds: Some(-(3600 + 30)) },
            &mut out,
        );
        assert_eq!(out, "01:02:03-01:00:30");
    }
}
