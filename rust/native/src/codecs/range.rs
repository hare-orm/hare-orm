//! A PostgreSQL range field: `RangeField.to_python()` - the driver's range read by its attributes,
//! the bounds coerced, a discrete range made `[lower, upper)`, a range holding no value made the
//! empty range; a timestamp range's bounds read into the configured zone. Text, a tuple, a naive or
//! out-of-range timestamp, a bound of another type go through the field.

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDate, PyDateAccess, PyDateTime, PyInt, PyString, PyTuple, PyType, PyTzInfoAccess};
use pyo3::{PyTraverseError, PyVisit};

use crate::codecs::datetime::with_tzinfo;
use crate::codecs::options::Options;
use crate::pg::result_cell::ResultCell;
use crate::pg::value::Value;
use crate::python::references::PythonReferences;
use crate::python::{ffi, objects};

/// The year of `datetime.min`.
const MINIMUM_YEAR: i32 = 1;
/// The year of `datetime.max`.
const MAXIMUM_YEAR: i32 = 9999;

/// The type of a range's bounds.
enum BoundType {
    Integer,
    Decimal,
    Date,
    /// A timestamp, read into `zone` under `use_timezone`.
    Datetime {
        use_timezone: bool,
        zone: Option<Py<PyAny>>,
        zone_is_utc: bool,
    },
}

/// A bound as this codec makes it, or left to the field.
enum BoundOutcome<'py> {
    /// The bound - None for an unbounded side.
    Made(Option<Bound<'py, PyAny>>),
    LeftToField,
}

/// A range's bounds and flags, as read or made.
struct RangeParts<'py> {
    lower: Option<Bound<'py, PyAny>>,
    upper: Option<Bound<'py, PyAny>>,
    lower_inc: bool,
    upper_inc: bool,
    is_empty: bool,
}

pub struct RangeRead {
    /// The `Range` class a field holds.
    range_type: Py<PyType>,
    bound: BoundType,
    /// The distance between neighbouring values of a discrete range, None for a continuous one.
    step: Option<Py<PyAny>>,
    fallback: Py<PyAny>,
}

impl RangeRead {
    pub fn from_options(options: &Options<'_>) -> PyResult<Self> {
        let py = options.py();
        let bound = match options.get_string("bound")?.as_str() {
            "integer" => BoundType::Integer,
            "decimal" => BoundType::Decimal,
            "date" => BoundType::Date,
            "datetime" => {
                let use_timezone = options.get_bool("use_timezone")?;
                BoundType::Datetime {
                    use_timezone,
                    zone: if use_timezone { Some(options.get_object("zone")?) } else { None },
                    zone_is_utc: options.get_bool("zone_is_utc")?,
                }
            }
            other => return Err(options.error(&format!("unknown range bound {other:?}"))),
        };
        Ok(RangeRead {
            range_type: options.get_object("range_type")?.into_bound(py).cast_into::<PyType>()?.unbind(),
            bound,
            step: options.get_optional_object("step")?,
            fallback: options.get_object("fallback")?,
        })
    }

    pub fn read<'py>(&self, raw: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = raw.py();
        if raw.is_none() {
            return Ok(raw);
        }
        if raw.is_instance_of::<PyString>() || raw.is_instance_of::<PyTuple>() {
            return self.fallback.bind(py).call1((raw,));
        }
        let Ok(parts) = self.get_parts(&raw) else {
            return self.fallback.bind(py).call1((raw,));
        };
        match self.canonicalize(py, parts)? {
            Some(parts) => self.new_range(py, &parts),
            None => self.fallback.bind(py).call1((raw,)),
        }
    }

    /// The value of `cell`, a value of a `rust.native.pg` result - what `read()` gives for the
    /// `Range` the driver would have returned, its parts taken from the decoded range itself.
    pub fn read_cell<'py>(&self, py: Python<'py>, cell: &ResultCell<'_>) -> PyResult<Bound<'py, PyAny>> {
        cell.with_value(|value| {
            let Value::Range(range) = value else {
                return self.read(value.into_pyobject(py)?);
            };
            let read_bound = |bound: &Option<Box<Value>>| -> PyResult<Option<Bound<'py, PyAny>>> {
                match bound {
                    Some(bound) if !range.empty => {
                        let bound = bound.as_ref().into_pyobject(py)?;
                        Ok((!bound.is_none()).then_some(bound))
                    }
                    _ => Ok(None),
                }
            };
            let parts = RangeParts {
                lower: read_bound(&range.lower)?,
                upper: read_bound(&range.upper)?,
                lower_inc: range.lower_inc && !range.empty,
                upper_inc: range.upper_inc && !range.empty,
                is_empty: range.empty,
            };
            match self.canonicalize(py, parts)? {
                Some(parts) => self.new_range(py, &parts),
                None => self.fallback.bind(py).call1((value.into_pyobject(py)?,)),
            }
        })?
    }

    /// The bounds and flags of a `Range`, or of the driver's own range object.
    fn get_parts<'py>(&self, raw: &Bound<'py, PyAny>) -> PyResult<RangeParts<'py>> {
        let py = raw.py();
        let is_empty_name =
            if raw.get_type().is(self.range_type.bind(py)) { intern!(py, "is_empty") } else { intern!(py, "isempty") };
        let lower = raw.getattr(intern!(py, "lower"))?;
        let upper = raw.getattr(intern!(py, "upper"))?;
        Ok(RangeParts {
            lower: (!lower.is_none()).then_some(lower),
            upper: (!upper.is_none()).then_some(upper),
            lower_inc: raw.getattr(intern!(py, "lower_inc"))?.is_truthy()?,
            upper_inc: raw.getattr(intern!(py, "upper_inc"))?.is_truthy()?,
            is_empty: raw.getattr(is_empty_name)?.is_truthy()?,
        })
    }

    /// The range as the field holds it - None when the field has to make it.
    fn canonicalize<'py>(&self, py: Python<'py>, parts: RangeParts<'py>) -> PyResult<Option<RangeParts<'py>>> {
        if parts.is_empty {
            return Ok(Some(RangeParts {
                lower: None,
                upper: None,
                lower_inc: false,
                upper_inc: false,
                is_empty: true,
            }));
        }
        let (BoundOutcome::Made(mut lower), BoundOutcome::Made(mut upper)) =
            (self.coerce(parts.lower)?, self.coerce(parts.upper)?)
        else {
            return Ok(None);
        };
        let mut lower_inc = parts.lower_inc && lower.is_some();
        let mut upper_inc = parts.upper_inc && upper.is_some();
        if let Some(step) = &self.step {
            let step = step.bind(py);
            if let Some(value) = &lower {
                if !lower_inc {
                    let Ok(next) = value.add(step) else {
                        return Ok(None);
                    };
                    lower = Some(next);
                    lower_inc = true;
                }
            }
            if let Some(value) = &upper {
                if upper_inc {
                    let Ok(next) = value.add(step) else {
                        return Ok(None);
                    };
                    upper = Some(next);
                    upper_inc = false;
                }
            }
        }
        if let (Some(lower_value), Some(upper_value)) = (&lower, &upper) {
            if lower_value.gt(upper_value)? {
                // The field raises its own error.
                return Ok(None);
            }
            if lower_value.eq(upper_value)? && !(lower_inc && upper_inc) {
                return Ok(Some(RangeParts {
                    lower: None,
                    upper: None,
                    lower_inc: false,
                    upper_inc: false,
                    is_empty: true,
                }));
            }
        }
        let (BoundOutcome::Made(lower), BoundOutcome::Made(upper)) = (self.read_bound(lower)?, self.read_bound(upper)?)
        else {
            return Ok(None);
        };
        Ok(Some(RangeParts { lower, upper, lower_inc, upper_inc, is_empty: false }))
    }

    /// `coerce_bound()` of a bound.
    fn coerce<'py>(&self, bound: Option<Bound<'py, PyAny>>) -> PyResult<BoundOutcome<'py>> {
        let Some(value) = bound else {
            return Ok(BoundOutcome::Made(None));
        };
        let py = value.py();
        let coerced = match &self.bound {
            BoundType::Integer => value.is_instance_of::<PyInt>().then_some(value),
            BoundType::Decimal => value.is_instance(objects::decimal_type(py)?)?.then_some(value),
            BoundType::Date => {
                if let Ok(datetime) = value.cast::<PyDateTime>() {
                    Some(PyDate::new(py, datetime.get_year(), datetime.get_month(), datetime.get_day())?.into_any())
                } else {
                    value.is_instance_of::<PyDate>().then_some(value)
                }
            }
            BoundType::Datetime { .. } => {
                let usable = value.cast::<PyDateTime>().is_ok_and(|datetime| {
                    let year = datetime.get_year();
                    year != MINIMUM_YEAR
                        && year != MAXIMUM_YEAR
                        && datetime
                            .get_tzinfo()
                            .is_some_and(|tzinfo| objects::has_offset_for_datetime(&tzinfo).unwrap_or(false))
                });
                usable.then_some(value)
            }
        };
        Ok(coerced.map_or(BoundOutcome::LeftToField, |value| BoundOutcome::Made(Some(value))))
    }

    /// A coerced bound as the field reads it - a timestamp in the configured zone.
    fn read_bound<'py>(&self, bound: Option<Bound<'py, PyAny>>) -> PyResult<BoundOutcome<'py>> {
        let BoundType::Datetime { use_timezone, zone, zone_is_utc } = &self.bound else {
            return Ok(BoundOutcome::Made(bound));
        };
        let Some(value) = bound else {
            return Ok(BoundOutcome::Made(None));
        };
        if !use_timezone {
            // The system's local time - the field knows the system zone.
            return Ok(BoundOutcome::LeftToField);
        }
        let py = value.py();
        let zone = zone.as_ref().expect("use_timezone carries a zone").bind(py);
        let datetime = value.cast::<PyDateTime>()?;
        let utc = objects::utc_timezone(py)?;
        if *zone_is_utc && datetime.get_tzinfo().is_some_and(|tzinfo| tzinfo.is(utc)) {
            return Ok(BoundOutcome::Made(Some(with_tzinfo(datetime, zone)?)));
        }
        match value.call_method1(intern!(py, "astimezone"), (zone,)) {
            Ok(converted) => Ok(BoundOutcome::Made(Some(converted))),
            Err(_) => Ok(BoundOutcome::LeftToField),
        }
    }

    /// A new `Range` of `parts` - built without the dataclass's `__init__`.
    fn new_range<'py>(&self, py: Python<'py>, parts: &RangeParts<'py>) -> PyResult<Bound<'py, PyAny>> {
        let range = ffi::allocate_instance(self.range_type.bind(py))?;
        let none = py.None().into_bound(py);
        ffi::generic_set_attribute(&range, intern!(py, "lower"), parts.lower.as_ref().unwrap_or(&none))?;
        ffi::generic_set_attribute(&range, intern!(py, "upper"), parts.upper.as_ref().unwrap_or(&none))?;
        ffi::generic_set_attribute(&range, intern!(py, "lower_inc"), PyBool::new(py, parts.lower_inc).as_any())?;
        ffi::generic_set_attribute(&range, intern!(py, "upper_inc"), PyBool::new(py, parts.upper_inc).as_any())?;
        ffi::generic_set_attribute(&range, intern!(py, "is_empty"), PyBool::new(py, parts.is_empty).as_any())?;
        Ok(range)
    }
}

impl PythonReferences for RangeRead {
    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.range_type)?;
        visit.call(&self.step)?;
        visit.call(&self.fallback)?;
        if let BoundType::Datetime { zone, .. } = &self.bound {
            visit.call(zone)?;
        }
        Ok(())
    }
}
