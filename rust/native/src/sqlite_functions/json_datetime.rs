//! An ISO-8601 date or datetime string at a JSON path, read as a JSON `__filter` date comparison
//! casts it on Postgres - whole microseconds since the Unix epoch of its wall clock (`wall`) or of
//! its instant (`instant`, no offset meaning UTC), or a date part of its wall clock. A text this
//! doesn't read, or a part past the last date, goes to the Python function.

use chrono::{Datelike, Duration, NaiveDate, NaiveTime};
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::PyString;
use pyo3::{PyTraverseError, PyVisit};

use crate::sqlite_functions::date_functions::get_date_part_number;

const MICROSECONDS_PER_SECOND: i64 = 1_000_000;
const MICROSECONDS_PER_DAY: i64 = 86_400 * MICROSECONDS_PER_SECOND;

/// The number of the ASCII digits at `start..start + length` if it is at most `maximum`.
fn read_digits(bytes: &[u8], start: usize, length: usize, maximum: i64) -> Option<i64> {
    let digits = bytes.get(start..start + length)?;
    if !digits.iter().all(u8::is_ascii_digit) {
        return None;
    }
    let number = digits.iter().fold(0_i64, |number, digit| number * 10 + i64::from(digit - b'0'));
    (number <= maximum).then_some(number)
}

/// Microseconds of a seconds field and its fraction digits, the fraction rounded half to even.
fn get_second_microseconds(seconds: i64, fraction: &[u8]) -> i64 {
    let mut microseconds = 0_i64;
    for index in 0..6 {
        microseconds = microseconds * 10 + fraction.get(index).map_or(0, |digit| i64::from(digit - b'0'));
    }
    if let Some((&first_dropped, rest)) = fraction.get(6..).and_then(<[u8]>::split_first) {
        let is_above_half = first_dropped > b'5' || (first_dropped == b'5' && rest.iter().any(|&digit| digit != b'0'));
        let is_half = first_dropped == b'5' && rest.iter().all(|&digit| digit == b'0');
        if is_above_half || (is_half && microseconds % 2 == 1) {
            microseconds += 1;
        }
    }
    seconds * MICROSECONDS_PER_SECOND + microseconds
}

/// What `parse()` read from a text.
enum IsoReading {
    /// The wall-clock microseconds since the epoch and the offset in microseconds, if the text has one.
    Moment { wall: i64, offset: Option<i64> },
    /// Text `JSON_ISO_DATETIME_PATTERN` doesn't match, or a date no calendar has.
    NotIso,
    /// Text the Python function reads.
    LeftToPython,
}

/// An ISO text in the form `JSON_ISO_DATETIME_PATTERN` matches.
fn parse(text: &str) -> IsoReading {
    if text.ends_with('\n') {
        return IsoReading::LeftToPython;
    }
    let bytes = text.as_bytes();
    let (Some(year), Some(month), Some(day)) =
        (read_digits(bytes, 0, 4, 9999), read_digits(bytes, 5, 2, 12), read_digits(bytes, 8, 2, 31))
    else {
        return IsoReading::NotIso;
    };
    if year == 0 || month == 0 || day == 0 || bytes[4] != b'-' || bytes[7] != b'-' {
        return IsoReading::NotIso;
    }
    let (Ok(year), Ok(month), Ok(day)) = (i32::try_from(year), u32::try_from(month), u32::try_from(day)) else {
        return IsoReading::NotIso;
    };
    let Some(date) = NaiveDate::from_ymd_opt(year, month, day) else {
        return IsoReading::NotIso;
    };
    let mut clock = 0;
    let mut offset = None;
    if bytes.len() > 10 {
        if !matches!(bytes[10], b'T' | b' ') || bytes.get(13) != Some(&b':') {
            return IsoReading::NotIso;
        }
        let (Some(hour), Some(minute)) = (read_digits(bytes, 11, 2, 23), read_digits(bytes, 14, 2, 59)) else {
            return IsoReading::NotIso;
        };
        let mut position = 16;
        let mut second_microseconds = 0;
        if bytes.get(position) == Some(&b':') {
            let Some(second) = read_digits(bytes, position + 1, 2, 59) else {
                return IsoReading::NotIso;
            };
            position += 3;
            let mut fraction: &[u8] = &[];
            if bytes.get(position) == Some(&b'.') {
                let start = position + 1;
                let mut end = start;
                while bytes.get(end).is_some_and(u8::is_ascii_digit) {
                    end += 1;
                }
                if end == start {
                    return IsoReading::NotIso;
                }
                fraction = &bytes[start..end];
                position = end;
            }
            second_microseconds = get_second_microseconds(second, fraction);
        }
        clock = (hour * 60 + minute) * 60 * MICROSECONDS_PER_SECOND + second_microseconds;
        match bytes.get(position) {
            None => {}
            Some(b'Z') if bytes.len() == position + 1 => offset = Some(0),
            Some(&sign @ (b'+' | b'-')) => {
                let Some(offset_hour) = read_digits(bytes, position + 1, 2, 23) else {
                    return IsoReading::NotIso;
                };
                let rest = &bytes[position + 3..];
                let offset_minute = match rest {
                    [] => 0,
                    [b':', tens, ones] | [tens, ones] => match read_digits(&[*tens, *ones], 0, 2, 59) {
                        Some(minute) => minute,
                        None => return IsoReading::NotIso,
                    },
                    _ => return IsoReading::NotIso,
                };
                let magnitude = (offset_hour * 60 + offset_minute) * 60 * MICROSECONDS_PER_SECOND;
                offset = Some(if sign == b'-' { -magnitude } else { magnitude });
            }
            Some(_) => return IsoReading::NotIso,
        }
    }
    let epoch = NaiveDate::from_ymd_opt(1970, 1, 1).expect("the epoch");
    let days = (date - epoch).num_days();
    IsoReading::Moment { wall: days * MICROSECONDS_PER_DAY + clock, offset }
}

/// The JSON date reading function; arguments it doesn't handle go to the Python one given.
#[pyclass(frozen, module = "rust.native.sqlite_functions")]
pub struct JsonDatetime {
    /// `SqliteJsonDatetime`, whose `normalize` reads what this doesn't.
    python_functions: Py<PyAny>,
}

#[pymethods]
impl JsonDatetime {
    #[new]
    fn new(python_functions: Py<PyAny>) -> Self {
        JsonDatetime { python_functions }
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.python_functions)
    }

    /// The microseconds or the date part of an ISO text in `mode`; None for anything else.
    fn normalize<'py>(&self, text: &Bound<'py, PyAny>, mode: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = text.py();
        let Ok(text_string) = text.cast::<PyString>() else {
            return Ok(py.None().into_bound(py));
        };
        let mode_text = mode.cast::<PyString>().ok().and_then(|mode| mode.to_str().ok());
        let reading = parse(text_string.to_str()?);
        if let (Some(mode_text), false) = (mode_text, matches!(reading, IsoReading::LeftToPython)) {
            let IsoReading::Moment { wall, offset } = reading else {
                return Ok(py.None().into_bound(py));
            };
            let result = match mode_text {
                "wall" => Some(wall),
                "instant" => Some(wall - offset.unwrap_or(0)),
                part => {
                    let days = wall.div_euclid(MICROSECONDS_PER_DAY);
                    let clock = wall.rem_euclid(MICROSECONDS_PER_DAY);
                    let epoch = NaiveDate::from_ymd_opt(1970, 1, 1).expect("the epoch");
                    epoch
                        .checked_add_signed(Duration::days(days))
                        .filter(|date| date.year() <= 9999)
                        .map(|date| date.and_time(NaiveTime::MIN) + Duration::microseconds(clock))
                        .and_then(|moment| get_date_part_number(part, moment))
                }
            };
            if let Some(result) = result {
                return Ok(result.into_pyobject(py)?.into_any());
            }
        }
        self.python_functions.bind(py).call_method1(intern!(py, "normalize"), (text, mode))
    }
}
