//! Python object -> `Value`: binding a query parameter.

use chrono::{DateTime, Datelike, FixedOffset, NaiveDate, NaiveDateTime, NaiveTime, TimeDelta, Utc};
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{
    PyBool, PyBytes, PyDate, PyDateAccess, PyDateTime, PyDelta, PyDeltaAccess, PyDict, PyFloat, PyInt, PyList,
    PyString, PyTime, PyTimeAccess, PyTzInfo,
};
use pyo3::Borrowed;

use crate::pg::error::{to_pyerr, DriverError};
use crate::pg::types::json::write_python_json_with_context;
use crate::pg::types::numeric::NonFiniteNumeric;
use crate::pg::value::python_classes::{is_ip_address_object, range_type};
use crate::pg::value::{IntervalValue, RangeValue, Value};
use crate::python::objects::{decimal_type, uuid_type};
use crate::python::value_traversal::PythonValueTraversal;

impl<'a, 'py> FromPyObject<'a, 'py> for Value {
    type Error = PyErr;

    fn extract(obj: Borrowed<'a, 'py, PyAny>) -> Result<Self, PyErr> {
        Self::extract_with_context(&obj, &mut PythonValueTraversal::default())
    }
}

impl Value {
    pub(crate) fn extract_with_context(
        object: &Bound<'_, PyAny>,
        context: &mut PythonValueTraversal,
    ) -> PyResult<Self> {
        if object.is_none() {
            return Ok(Value::Null);
        }
        // bool must be checked before int (bool is a subclass of int in Python).
        if object.is_instance_of::<PyBool>() {
            return Ok(Value::Bool(object.extract::<bool>()?));
        }
        if object.is_instance_of::<PyInt>() {
            return Ok(match object.extract::<i64>() {
                Ok(value) => Value::Int(value),
                Err(_) => Value::BigInt(object.call_method0(intern!(object.py(), "__index__"))?.str()?.to_string()),
            });
        }
        if object.is_instance_of::<PyFloat>() {
            return Ok(Value::Float(object.extract::<f64>()?));
        }
        if object.is_instance_of::<PyString>() {
            return Ok(Value::Text(object.extract::<String>()?));
        }
        // datetime must be checked before date (datetime subclasses date).
        if object.is_instance_of::<PyDateTime>() {
            let datetime = object.cast::<PyDateTime>()?;
            let offset = object.call_method0(intern!(object.py(), "utcoffset"))?;
            if offset.is_none() {
                let wall_clock = get_wall_clock_datetime(datetime)
                    .ok_or_else(|| to_pyerr(DriverError::Conversion("datetime components are out of range".into())))?;
                return Ok(Value::Timestamp(wall_clock));
            }
            let offset = offset.cast::<PyDelta>()?;
            if let Some(instant) = get_utc_instant(datetime, offset) {
                return Ok(Value::TimestampTz(instant));
            }
            // Preserve Python's range handling when the UTC instant is outside datetime's range.
            let utc = object.call_method1(intern!(object.py(), "astimezone"), (PyTzInfo::utc(object.py())?,))?;
            return Ok(Value::TimestampTz(utc.extract::<DateTime<Utc>>()?));
        }
        if object.is_instance_of::<PyDate>() {
            return Ok(Value::Date(object.extract::<NaiveDate>()?));
        }
        if object.is_instance_of::<PyTime>() {
            let naive = object.extract::<NaiveTime>()?;
            let offset = object.call_method0(intern!(object.py(), "utcoffset"))?;
            if !offset.is_none() {
                let delta = offset.cast::<PyDelta>()?;
                let seconds = i64::from(delta.get_days()) * 86_400 + i64::from(delta.get_seconds());
                let offset = i32::try_from(seconds)
                    .ok()
                    .and_then(FixedOffset::east_opt)
                    .ok_or_else(|| to_pyerr(DriverError::Conversion("time offset is out of range".into())))?;
                return Ok(Value::TimeTz(naive, offset));
            }
            return Ok(Value::Time(naive));
        }
        if object.is_instance_of::<PyBytes>() {
            return Ok(Value::Bytes(object.extract::<Vec<u8>>()?));
        }
        if object.is_instance_of::<PyDelta>() {
            let delta = object.cast::<PyDelta>()?;
            let microseconds = i64::from(delta.get_seconds()) * 1_000_000 + i64::from(delta.get_microseconds());
            return Ok(Value::Interval(IntervalValue { months: 0, days: delta.get_days(), microseconds }));
        }
        let py = object.py();
        // UUID primary keys and Decimal money fields are the common case among the remaining
        // parameter types, so they are checked before Range/list/dict. Conversion failures go
        // through DriverError -> to_pyerr() so the Python side translates them like any other.
        if object.is_instance(uuid_type(py)?.as_any())? {
            return Ok(Value::Uuid(uuid::Uuid::from_u128(object.getattr(intern!(py, "int"))?.extract::<u128>()?)));
        }
        if object.is_instance(decimal_type(py)?.as_any())? {
            let text = object.str()?.to_string();
            // A finite Decimal's text holds digits, a point, an exponent and signs only.
            if text.contains(['N', 'n', 'I', 'i']) {
                return NonFiniteNumeric::from_decimal_text(&text)
                    .map(Value::NonFiniteDecimal)
                    .ok_or_else(|| to_pyerr(DriverError::Conversion(format!("unsupported decimal {text:?}"))));
            }
            return Ok(Value::Decimal(text));
        }
        if object.is_instance(range_type(py)?.as_any())? {
            return context.visit(object, |context| extract_range(object, context));
        }
        if object.is_instance_of::<PyList>() {
            return context.visit(object, |context| {
                // A list subclass may iterate a different sequence than its stored elements.
                let capacity = if object.is_exact_instance_of::<PyList>() { object.cast::<PyList>()?.len() } else { 0 };
                let mut items = Vec::with_capacity(capacity);
                for item in object.try_iter()? {
                    items.push(Self::extract_with_context(&item?, context)?);
                }
                Ok(Value::Array(items))
            });
        }
        if object.is_instance_of::<PyDict>() {
            let mut text = String::new();
            write_python_json_with_context(object, &mut text, context)?;
            return Ok(Value::Json(text));
        }
        // An ipaddress object is sent as its text, which the server parses as inet/cidr.
        if is_ip_address_object(object)? {
            return Ok(Value::Text(object.str()?.to_string()));
        }
        Err(to_pyerr(DriverError::Conversion(format!("unsupported parameter type: {}", object.get_type().name()?))))
    }
}

/// The wall-clock components, independent of whether tzinfo supplies an offset.
fn get_wall_clock_datetime(datetime: &Bound<'_, PyDateTime>) -> Option<NaiveDateTime> {
    NaiveDate::from_ymd_opt(datetime.get_year(), datetime.get_month().into(), datetime.get_day().into())?
        .and_hms_micro_opt(
            datetime.get_hour().into(),
            datetime.get_minute().into(),
            datetime.get_second().into(),
            datetime.get_microsecond(),
        )
}

/// The UTC instant of an aware datetime at `offset` - None past the range of a Python datetime.
fn get_utc_instant(datetime: &Bound<'_, PyDateTime>, offset: &Bound<'_, PyDelta>) -> Option<DateTime<Utc>> {
    let wall_clock = get_wall_clock_datetime(datetime)?;
    let offset = TimeDelta::days(offset.get_days().into())
        + TimeDelta::seconds(offset.get_seconds().into())
        + TimeDelta::microseconds(offset.get_microseconds().into());
    let instant = wall_clock.checked_sub_signed(offset)?;
    (1..=9999).contains(&instant.year()).then(|| instant.and_utc())
}

/// Extracts a `hare.dialects.postgresql.fields.ranges.Range(lower, upper, lower_inc, upper_inc, is_empty)`
/// dataclass instance into a `Value::Range`.
fn extract_range(object: &Bound<'_, PyAny>, context: &mut PythonValueTraversal) -> PyResult<Value> {
    let py = object.py();
    let lower = object.getattr(intern!(py, "lower"))?;
    let upper = object.getattr(intern!(py, "upper"))?;
    let lower_inc = object.getattr(intern!(py, "lower_inc"))?.extract::<bool>()?;
    let upper_inc = object.getattr(intern!(py, "upper_inc"))?.extract::<bool>()?;
    let empty = object.getattr(intern!(py, "is_empty"))?.extract::<bool>()?;
    let lower = if lower.is_none() { None } else { Some(Box::new(Value::extract_with_context(&lower, context)?)) };
    let upper = if upper.is_none() { None } else { Some(Box::new(Value::extract_with_context(&upper, context)?)) };
    Ok(Value::Range(RangeValue { lower, upper, lower_inc, upper_inc, empty }))
}
