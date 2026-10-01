//! Python object -> `Value`: binding a query parameter.

use chrono::{DateTime, Datelike, FixedOffset, NaiveDate, NaiveDateTime, NaiveTime, TimeDelta, Utc};
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{
    PyBool, PyBytes, PyDate, PyDateAccess, PyDateTime, PyDelta, PyDeltaAccess, PyDict, PyFloat, PyInt, PyList,
    PyString, PyTime, PyTimeAccess, PyTzInfo, PyTzInfoAccess,
};
use pyo3::Borrowed;

use crate::pg::error::{to_pyerr, DriverError};
use crate::pg::types::json::write_python_json;
use crate::pg::types::numeric::NonFiniteNumeric;
use crate::pg::value::python_classes::{is_ip_address_object, range_type};
use crate::pg::value::{IntervalValue, RangeValue, Value};
use crate::python::objects::{decimal_type, uuid_type};

impl<'a, 'py> FromPyObject<'a, 'py> for Value {
    type Error = PyErr;

    fn extract(obj: Borrowed<'a, 'py, PyAny>) -> Result<Self, PyErr> {
        let ob: &Bound<'py, PyAny> = &obj;
        if ob.is_none() {
            return Ok(Value::Null);
        }
        // bool must be checked before int (bool is a subclass of int in Python).
        if ob.is_instance_of::<PyBool>() {
            return Ok(Value::Bool(ob.extract::<bool>()?));
        }
        if ob.is_instance_of::<PyInt>() {
            return Ok(match ob.extract::<i64>() {
                Ok(value) => Value::Int(value),
                Err(_) => Value::BigInt(ob.call_method0("__index__")?.str()?.to_string()),
            });
        }
        if ob.is_instance_of::<PyFloat>() {
            return Ok(Value::Float(ob.extract::<f64>()?));
        }
        if ob.is_instance_of::<PyString>() {
            return Ok(Value::Text(ob.extract::<String>()?));
        }
        // datetime must be checked before date (datetime subclasses date).
        if ob.is_instance_of::<PyDateTime>() {
            if let Ok(dt) = ob.extract::<DateTime<FixedOffset>>() {
                return Ok(Value::TimestampTz(dt.with_timezone(&Utc)));
            }
            // A variable-offset tzinfo (zoneinfo.ZoneInfo of any zone with DST) fails the
            // extraction above - its offset at this instant is asked of the tzinfo.
            let datetime = ob.cast::<PyDateTime>()?;
            if datetime.get_tzinfo().is_some() {
                let offset = ob.call_method0("utcoffset")?;
                if let Ok(offset) = offset.cast::<PyDelta>() {
                    if let Some(instant) = get_utc_instant(datetime, offset) {
                        return Ok(Value::TimestampTz(instant));
                    }
                }
                // No offset (system local time), or an instant past the datetime range - as
                // Python converts it, or fails to.
                let utc = ob.call_method1("astimezone", (PyTzInfo::utc(ob.py())?,))?;
                return Ok(Value::TimestampTz(utc.extract::<DateTime<Utc>>()?));
            }
            return Ok(Value::Timestamp(ob.extract::<NaiveDateTime>()?));
        }
        if ob.is_instance_of::<PyDate>() {
            return Ok(Value::Date(ob.extract::<NaiveDate>()?));
        }
        if ob.is_instance_of::<PyTime>() {
            let naive = ob.extract::<NaiveTime>()?;
            if let Some(tzinfo) = ob.cast::<PyTime>()?.get_tzinfo() {
                return Ok(Value::TimeTz(naive, tzinfo.extract::<FixedOffset>()?));
            }
            return Ok(Value::Time(naive));
        }
        if ob.is_instance_of::<PyBytes>() {
            return Ok(Value::Bytes(ob.extract::<Vec<u8>>()?));
        }
        if ob.is_instance_of::<PyDelta>() {
            let delta = ob.cast::<PyDelta>()?;
            let microseconds = i64::from(delta.get_seconds()) * 1_000_000 + i64::from(delta.get_microseconds());
            return Ok(Value::Interval(IntervalValue { months: 0, days: delta.get_days(), microseconds }));
        }
        let py = ob.py();
        // UUID primary keys and Decimal money fields are the common case among the remaining
        // parameter types, so they are checked before Range/list/dict. Conversion failures go
        // through DriverError -> to_pyerr() so the Python side translates them like any other.
        if ob.is_instance(uuid_type(py)?.as_any())? {
            return Ok(Value::Uuid(uuid::Uuid::from_u128(ob.getattr(intern!(py, "int"))?.extract::<u128>()?)));
        }
        if ob.is_instance(decimal_type(py)?.as_any())? {
            let text = ob.str()?.to_string();
            // A finite Decimal's text holds digits, a point, an exponent and signs only.
            if text.contains(['N', 'n', 'I', 'i']) {
                return NonFiniteNumeric::from_decimal_text(&text)
                    .map(Value::NonFiniteDecimal)
                    .ok_or_else(|| to_pyerr(DriverError::Conversion(format!("unsupported decimal {text:?}"))));
            }
            return Ok(Value::Decimal(text));
        }
        if ob.is_instance(range_type(py)?.as_any())? {
            return extract_range(ob);
        }
        if ob.is_instance_of::<PyList>() {
            let mut items = Vec::new();
            for item in ob.try_iter()? {
                items.push(item?.extract::<Value>()?);
            }
            return Ok(Value::Array(items));
        }
        if ob.is_instance_of::<PyDict>() {
            let mut text = String::new();
            write_python_json(ob, &mut text)?;
            return Ok(Value::Json(text));
        }
        // An ipaddress object is sent as its text, which the server parses as inet/cidr.
        if is_ip_address_object(ob)? {
            return Ok(Value::Text(ob.str()?.to_string()));
        }
        Err(to_pyerr(DriverError::Conversion(format!("unsupported parameter type: {}", ob.get_type().name()?))))
    }
}

/// The UTC instant of an aware datetime at `offset` - None past the range of a Python datetime.
fn get_utc_instant(datetime: &Bound<'_, PyDateTime>, offset: &Bound<'_, PyDelta>) -> Option<DateTime<Utc>> {
    let wall_clock =
        NaiveDate::from_ymd_opt(datetime.get_year(), datetime.get_month().into(), datetime.get_day().into())?
            .and_hms_micro_opt(
                datetime.get_hour().into(),
                datetime.get_minute().into(),
                datetime.get_second().into(),
                datetime.get_microsecond(),
            )?;
    let offset = TimeDelta::days(offset.get_days().into())
        + TimeDelta::seconds(offset.get_seconds().into())
        + TimeDelta::microseconds(offset.get_microseconds().into());
    let instant = wall_clock.checked_sub_signed(offset)?;
    (1..=9999).contains(&instant.year()).then(|| instant.and_utc())
}

/// Extracts a `hare.dialects.postgresql.fields.ranges.Range(lower, upper, lower_inc, upper_inc, is_empty)`
/// dataclass instance into a `Value::Range`.
fn extract_range(ob: &Bound<'_, PyAny>) -> PyResult<Value> {
    let lower = ob.getattr("lower")?;
    let upper = ob.getattr("upper")?;
    let lower_inc = ob.getattr("lower_inc")?.extract::<bool>()?;
    let upper_inc = ob.getattr("upper_inc")?.extract::<bool>()?;
    let empty = match ob.getattr("is_empty") {
        Ok(value) => value.extract::<bool>()?,
        Err(_) => false,
    };
    let lower = if lower.is_none() { None } else { Some(Box::new(lower.extract::<Value>()?)) };
    let upper = if upper.is_none() { None } else { Some(Box::new(upper.extract::<Value>()?)) };
    Ok(Value::Range(RangeValue { lower, upper, lower_inc, upper_inc, empty }))
}
