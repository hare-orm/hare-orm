//! `Value` -> Python object: reading a result row.

use chrono::Timelike;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDelta, PyInt, PyList, PyTime, PyTuple};

use crate::pg::error::{to_pyerr, DriverError};
use crate::pg::types::interval::{DAYS_PER_INTERVAL_MONTH, DAYS_PER_INTERVAL_YEAR};
use crate::pg::value::python_classes::{
    range_type, IPV4_ADDRESS_TYPE, IPV4_INTERFACE_TYPE, IPV4_NETWORK_TYPE, IPV6_ADDRESS_TYPE, IPV6_INTERFACE_TYPE,
    IPV6_NETWORK_TYPE,
};
use crate::pg::value::{IntervalValue, NetworkValue, RangeValue, Value};
use crate::python::objects::{decimal_type, new_uuid};

impl<'py> IntoPyObject<'py> for Value {
    type Target = PyAny;
    type Output = Bound<'py, PyAny>;
    type Error = PyErr;

    fn into_pyobject(self, py: Python<'py>) -> Result<Self::Output, Self::Error> {
        let obj = match self {
            Value::Null => py.None().into_bound(py),
            Value::Bool(v) => v.into_pyobject(py)?.to_owned().into_any(),
            Value::Int(v) => v.into_pyobject(py)?.into_any(),
            Value::BigInt(text) => py.get_type::<PyInt>().call1((text,))?,
            Value::Float(v) => v.into_pyobject(py)?.into_any(),
            Value::Text(v) | Value::Json(v) => v.into_pyobject(py)?.into_any(),
            Value::Bytes(v) => PyBytes::new(py, &v).into_any(),
            Value::Array(items) => {
                let list = PyList::empty(py);
                for item in items {
                    list.append(item.into_pyobject(py)?)?;
                }
                list.into_any()
            }
            Value::Range(r) => range_to_py(py, r)?,
            Value::Uuid(v) => new_uuid(py, v.as_u128())?,
            Value::Decimal(text) => decimal_type(py)?.call1((text,))?.into_any(),
            Value::NonFiniteDecimal(v) => decimal_type(py)?.call1((v.as_text(),))?.into_any(),
            Value::Timestamp(v) => v.into_pyobject(py)?.into_any(),
            Value::TimestampTz(v) => v.into_pyobject(py)?.into_any(),
            Value::Date(v) => v.into_pyobject(py)?.into_any(),
            Value::Time(v) => v.into_pyobject(py)?.into_any(),
            Value::TimeTz(v, offset) => {
                let tzinfo = offset.into_pyobject(py)?;
                PyTime::new(
                    py,
                    v.hour() as u8,
                    v.minute() as u8,
                    v.second() as u8,
                    v.nanosecond() / 1000,
                    Some(&tzinfo),
                )?
                .into_any()
            }
            Value::Interval(interval) => interval_to_py(py, interval)?,
            Value::Network(network) => network_to_py(py, network)?,
            Value::Composite(items) => {
                let converted = items.into_iter().map(|item| item.into_pyobject(py)).collect::<PyResult<Vec<_>>>()?;
                PyTuple::new(py, converted)?.into_any()
            }
        };
        Ok(obj)
    }
}

/// Builds a `hare.dialects.postgresql.fields.ranges.Range(lower, upper, lower_inc, upper_inc, is_empty)`.
fn range_to_py(py: Python<'_>, r: RangeValue) -> PyResult<Bound<'_, PyAny>> {
    let cls = range_type(py)?;
    if r.empty {
        return Ok(cls.call1((py.None(), py.None(), false, false, true))?.into_any());
    }
    let lower = match r.lower {
        Some(v) => v.into_pyobject(py)?,
        None => py.None().into_bound(py),
    };
    let upper = match r.upper {
        Some(v) => v.into_pyobject(py)?,
        None => py.None().into_bound(py),
    };
    Ok(cls.call1((lower, upper, r.lower_inc, r.upper_inc))?.into_any())
}

/// An interval as a `timedelta`, the way asyncpg converts it: a year is 365 days, a month 30.
fn interval_to_py(py: Python<'_>, interval: IntervalValue) -> PyResult<Bound<'_, PyAny>> {
    let months = i64::from(interval.months);
    let calendar_days = months / 12 * DAYS_PER_INTERVAL_YEAR + months % 12 * DAYS_PER_INTERVAL_MONTH;
    let seconds = interval.microseconds.div_euclid(1_000_000);
    let microseconds = interval.microseconds.rem_euclid(1_000_000);
    let days = i64::from(interval.days) + calendar_days + seconds.div_euclid(86_400);
    let days = i32::try_from(days).map_err(|_| {
        to_pyerr(DriverError::Conversion(format!("interval of {days} days is out of range for a Python timedelta")))
    })?;
    Ok(PyDelta::new(py, days, seconds.rem_euclid(86_400) as i32, microseconds as i32, true)?.into_any())
}

/// An `INET` value as `ipaddress.ip_address`/`ip_interface` (a host address has no prefix
/// suffix), a `CIDR` value as `ipaddress.ip_network` - asyncpg's own mapping.
fn network_to_py(py: Python<'_>, network: NetworkValue) -> PyResult<Bound<'_, PyAny>> {
    let is_v4 = network.address.is_ipv4();
    let full_prefix_length = if is_v4 { 32 } else { 128 };
    let (class, text) = if network.is_cidr {
        let class = if is_v4 {
            IPV4_NETWORK_TYPE.import(py, "ipaddress", "IPv4Network")?
        } else {
            IPV6_NETWORK_TYPE.import(py, "ipaddress", "IPv6Network")?
        };
        (class, format!("{}/{}", network.address, network.prefix_length))
    } else if network.prefix_length == full_prefix_length {
        let class = if is_v4 {
            IPV4_ADDRESS_TYPE.import(py, "ipaddress", "IPv4Address")?
        } else {
            IPV6_ADDRESS_TYPE.import(py, "ipaddress", "IPv6Address")?
        };
        (class, network.address.to_string())
    } else {
        let class = if is_v4 {
            IPV4_INTERFACE_TYPE.import(py, "ipaddress", "IPv4Interface")?
        } else {
            IPV6_INTERFACE_TYPE.import(py, "ipaddress", "IPv6Interface")?
        };
        (class, format!("{}/{}", network.address, network.prefix_length))
    };
    Ok(class.call1((text,))?.into_any())
}
