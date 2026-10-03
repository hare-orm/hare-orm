//! The math functions `MathFunction` renders on SQLite, matching Postgres: an integer stays an
//! integer where Postgres keeps one, `MOD` takes the dividend's sign. Each computes what the Python
//! one does with the same C library functions; an argument this doesn't read, or a result where
//! the Python one raises (outside a function's domain, an overflow), goes to the Python function.

use pyo3::call::PyCallArgs;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFloat, PyInt, PyString};
use pyo3::{PyTraverseError, PyVisit};

use crate::sqlite_functions::decimal_number::DecimalNumber;

/// A number argument.
enum Number {
    Integer(i128),
    Float(f64),
    /// A `DecimalField`'s text, with the float `float(Decimal(text))` makes of it.
    Decimal(DecimalNumber, f64),
}

impl Number {
    /// The number of an argument; None for one this doesn't read (NULL, text `Decimal()` reads
    /// otherwise, a BLOB).
    fn read(value: &Bound<'_, PyAny>) -> Option<Self> {
        if value.is_instance_of::<PyInt>() {
            return value.extract().ok().map(Number::Integer);
        }
        if let Ok(float) = value.cast::<PyFloat>() {
            return Some(Number::Float(float.value()));
        }
        let text = value.cast::<PyString>().ok()?.to_str().ok()?;
        let number = DecimalNumber::parse(text)?;
        Some(Number::Decimal(number, text.parse().ok()?))
    }

    fn to_float(&self) -> f64 {
        match self {
            #[expect(clippy::cast_precision_loss, reason = "float() of an int rounds the same way")]
            Number::Integer(integer) => *integer as f64,
            Number::Float(float) | Number::Decimal(_, float) => *float,
        }
    }
}

/// The 64-bit integer SQLite holds of a whole number - None past 64 bits.
fn to_sqlite_integer(integer: i128) -> Option<i64> {
    i64::try_from(integer).ok()
}

/// A decimal rounded to a whole number up (`ceiling`) or down; None past 128 bits.
fn round_decimal(number: &DecimalNumber, ceiling: bool) -> Option<i128> {
    let DecimalNumber::Finite { negative, digits, exponent } = number else {
        return Some(0);
    };
    let whole_digits = usize::try_from(exponent + 1).unwrap_or(0);
    if whole_digits > 38 {
        return None;
    }
    let mut whole: i128 = 0;
    for index in 0..whole_digits {
        whole = whole * 10 + i128::from(digits.get(index).map_or(0, |digit| digit - b'0'));
    }
    let has_fraction = digits.len() > whole_digits;
    let signed = if *negative { -whole } else { whole };
    Some(match (has_fraction, ceiling, *negative) {
        (true, true, false) => signed + 1,
        (true, false, true) => signed - 1,
        _ => signed,
    })
}

/// The math functions; arguments they don't handle go to the Python ones given.
#[pyclass(frozen, module = "rust.native.sqlite_functions")]
pub struct MathFunctions {
    /// Name -> `(argument count, Python function)`, as `SqliteMathFunctions.get_functions()`.
    python_functions: Py<PyDict>,
}

impl MathFunctions {
    /// The Python function's result for `arguments`.
    fn call_python<'py>(
        &self,
        py: Python<'py>,
        name: &str,
        arguments: impl PyCallArgs<'py>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let entry = self.python_functions.bind(py).get_item(name)?.expect("a registered math function");
        entry.get_item(1)?.call1(arguments)
    }

    /// A float result, or the Python function's where CPython's math module raises for it - NaN of
    /// arguments that aren't NaN, an infinity of finite arguments.
    fn checked_or_python<'py>(
        &self,
        py: Python<'py>,
        name: &str,
        result: f64,
        arguments: &[f64],
        python_arguments: impl PyCallArgs<'py>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let is_domain_error = result.is_nan() && !arguments.iter().any(|argument| argument.is_nan());
        let is_overflow = result.is_infinite() && arguments.iter().all(|argument| argument.is_finite());
        if is_domain_error || is_overflow {
            return self.call_python(py, name, python_arguments);
        }
        Ok(PyFloat::new(py, result).into_any())
    }

    /// A one-argument float function.
    fn unary<'py>(
        &self,
        name: &str,
        value: &Bound<'py, PyAny>,
        function: fn(f64) -> f64,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        if value.is_none() {
            return Ok(py.None().into_bound(py));
        }
        match Number::read(value) {
            Some(number) => {
                let argument = number.to_float();
                if !argument.is_finite() {
                    // CPython's own special cases decide.
                    return self.call_python(py, name, (value,));
                }
                self.checked_or_python(py, name, function(argument), &[argument], (value,))
            }
            None => self.call_python(py, name, (value,)),
        }
    }
}

#[pymethods]
impl MathFunctions {
    #[new]
    fn new(python_functions: Py<PyDict>) -> Self {
        MathFunctions { python_functions }
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.python_functions)
    }

    fn abs<'py>(&self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        if value.is_none() {
            return Ok(py.None().into_bound(py));
        }
        match Number::read(value) {
            Some(Number::Integer(integer)) => match to_sqlite_integer(integer.abs()) {
                Some(result) => Ok(result.into_pyobject(py)?.into_any()),
                None => self.call_python(py, "abs", (value,)),
            },
            Some(number) => Ok(PyFloat::new(py, number.to_float().abs()).into_any()),
            None => self.call_python(py, "abs", (value,)),
        }
    }

    fn ceil<'py>(&self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.round_to_integral("ceil", value, true)
    }

    fn floor<'py>(&self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.round_to_integral("floor", value, false)
    }

    fn sign<'py>(&self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        if value.is_none() {
            return Ok(py.None().into_bound(py));
        }
        let sign = match Number::read(value) {
            Some(Number::Integer(integer)) => integer.signum() as i64,
            Some(Number::Float(float)) if !float.is_nan() => i64::from(float > 0.0) - i64::from(float < 0.0),
            Some(Number::Decimal(DecimalNumber::Zero, _)) => 0,
            Some(Number::Decimal(DecimalNumber::Finite { negative, .. }, _)) => {
                if negative {
                    -1
                } else {
                    1
                }
            }
            _ => return self.call_python(py, "sign", (value,)),
        };
        Ok(sign.into_pyobject(py)?.into_any())
    }

    #[pyo3(name = "mod")]
    fn remainder<'py>(&self, dividend: &Bound<'py, PyAny>, divisor: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = dividend.py();
        if dividend.is_none() || divisor.is_none() {
            return Ok(py.None().into_bound(py));
        }
        match (Number::read(dividend), Number::read(divisor)) {
            (Some(Number::Integer(left)), Some(Number::Integer(right))) if right != 0 => {
                let result = left.abs() % right.abs();
                let result = if left >= 0 { result } else { -result };
                Ok(result.into_pyobject(py)?.into_any())
            }
            (Some(left @ Number::Float(_)), Some(right)) | (Some(left), Some(right @ Number::Float(_)))
                if right.to_float() != 0.0 && left.to_float().is_finite() && !right.to_float().is_nan() =>
            {
                Ok(PyFloat::new(py, left.to_float() % right.to_float()).into_any())
            }
            _ => self.call_python(py, "mod", (dividend, divisor)),
        }
    }

    fn power<'py>(&self, base: &Bound<'py, PyAny>, exponent: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = base.py();
        if base.is_none() || exponent.is_none() {
            return Ok(py.None().into_bound(py));
        }
        match (Number::read(base), Number::read(exponent)) {
            (Some(base_number), Some(exponent_number)) => {
                let (base_float, exponent_float) = (base_number.to_float(), exponent_number.to_float());
                if !base_float.is_finite() || !exponent_float.is_finite() {
                    return self.call_python(py, "power", (base, exponent));
                }
                let result = base_float.powf(exponent_float);
                self.checked_or_python(py, "power", result, &[base_float, exponent_float], (base, exponent))
            }
            _ => self.call_python(py, "power", (base, exponent)),
        }
    }

    #[expect(clippy::float_cmp, reason = "Python raises for a base of exactly 1")]
    fn log<'py>(&self, base: &Bound<'py, PyAny>, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = base.py();
        if base.is_none() || value.is_none() {
            return Ok(py.None().into_bound(py));
        }
        if let (Some(base_number), Some(number)) = (Number::read(base), Number::read(value)) {
            let (base_float, float) = (base_number.to_float(), number.to_float());
            if base_float > 0.0 && float > 0.0 && base_float != 1.0 && base_float.is_finite() && float.is_finite() {
                return Ok(PyFloat::new(py, float.ln() / base_float.ln()).into_any());
            }
        }
        self.call_python(py, "log", (base, value))
    }

    fn sqrt<'py>(&self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.unary("sqrt", value, f64::sqrt)
    }

    fn exp<'py>(&self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.unary("exp", value, f64::exp)
    }

    fn ln<'py>(&self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.unary("ln", value, f64::ln)
    }

    fn sin<'py>(&self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.unary("sin", value, f64::sin)
    }

    fn cos<'py>(&self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.unary("cos", value, f64::cos)
    }

    fn tan<'py>(&self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.unary("tan", value, f64::tan)
    }

    fn asin<'py>(&self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.unary("asin", value, f64::asin)
    }

    fn acos<'py>(&self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.unary("acos", value, f64::acos)
    }

    fn atan<'py>(&self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.unary("atan", value, f64::atan)
    }

    fn cot<'py>(&self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        if value.is_none() {
            return Ok(py.None().into_bound(py));
        }
        match Number::read(value).map(|number| number.to_float()) {
            Some(argument) if argument.is_finite() => {
                let tangent = argument.tan();
                Ok(PyFloat::new(py, if tangent == 0.0 { f64::INFINITY } else { 1.0 / tangent }).into_any())
            }
            _ => self.call_python(py, "cot", (value,)),
        }
    }

    fn atan2<'py>(&self, y: &Bound<'py, PyAny>, x: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = y.py();
        if y.is_none() || x.is_none() {
            return Ok(py.None().into_bound(py));
        }
        match (Number::read(y).map(|number| number.to_float()), Number::read(x).map(|number| number.to_float())) {
            (Some(y_float), Some(x_float)) if y_float.is_finite() && x_float.is_finite() => {
                Ok(PyFloat::new(py, y_float.atan2(x_float)).into_any())
            }
            _ => self.call_python(py, "atan2", (y, x)),
        }
    }

    #[staticmethod]
    fn pi(py: Python<'_>) -> Bound<'_, PyAny> {
        PyFloat::new(py, std::f64::consts::PI).into_any()
    }

    fn degrees<'py>(&self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.unary("degrees", value, f64::to_degrees)
    }

    fn radians<'py>(&self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.unary("radians", value, f64::to_radians)
    }
}

impl MathFunctions {
    fn round_to_integral<'py>(
        &self,
        name: &str,
        value: &Bound<'py, PyAny>,
        ceiling: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        if value.is_none() {
            return Ok(py.None().into_bound(py));
        }
        match Number::read(value) {
            Some(Number::Integer(integer)) => match to_sqlite_integer(integer) {
                Some(result) => Ok(result.into_pyobject(py)?.into_any()),
                None => self.call_python(py, name, (value,)),
            },
            Some(Number::Float(float)) => {
                // float(math.ceil(x)) goes through an int - no negative zero.
                let result = if !float.is_finite() {
                    float
                } else if ceiling {
                    float.ceil() + 0.0
                } else {
                    float.floor() + 0.0
                };
                Ok(PyFloat::new(py, result).into_any())
            }
            Some(Number::Decimal(number, _)) => match round_decimal(&number, ceiling).and_then(to_sqlite_integer) {
                Some(result) => Ok(result.into_pyobject(py)?.into_any()),
                None => self.call_python(py, name, (value,)),
            },
            None => self.call_python(py, name, (value,)),
        }
    }
}
