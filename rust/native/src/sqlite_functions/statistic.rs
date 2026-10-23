//! The variance and standard deviation aggregates - also window functions - on SQLite: the values of
//! the rows gathered here, the statistic of them worked out by the Python function given, once per
//! group or frame.

use std::sync::Mutex;

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyFloat, PyInt, PyList};
use pyo3::{PyTraverseError, PyVisit};

/// One aggregate - the values of its group or window frame so far.
#[pyclass(frozen, module = "rust.native.sqlite_functions")]
pub struct Statistic {
    sample: bool,
    is_deviation: bool,
    /// `(values, sample, is_deviation) -> float | None`.
    compute: Py<PyAny>,
    /// `decimal.Decimal`, which reads a `DecimalField`'s text.
    decimal_type: Py<PyAny>,
    values: Mutex<Vec<Py<PyAny>>>,
}

impl Statistic {
    /// A row's value as a number - a `DecimalField`'s text as a Decimal.
    fn get_number<'py>(&self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        if value.is_instance_of::<PyInt>() || value.is_instance_of::<PyFloat>() {
            return Ok(value.clone());
        }
        let decimal_type = self.decimal_type.bind(value.py());
        if value.is_instance_of::<PyBytes>() {
            return decimal_type.call1((value.call_method0(intern!(value.py(), "decode"))?,));
        }
        decimal_type.call1((value,))
    }

    /// The statistic of integers, exactly as `statistics` works it out - None for other values, or
    /// sums past 128 bits, which the Python function takes.
    fn get_integer_statistic<'py>(&self, py: Python<'py>, values: &[Py<PyAny>]) -> PyResult<Option<Bound<'py, PyAny>>> {
        let minimum_count = if self.sample { 2 } else { 1 };
        if values.len() < minimum_count {
            return Ok(Some(py.None().into_bound(py)));
        }
        let mut sum: i128 = 0;
        let mut sum_of_squares: i128 = 0;
        for value in values {
            let value = value.bind(py);
            if !value.is_exact_instance_of::<PyInt>() {
                return Ok(None);
            }
            let Ok(number) = value.extract::<i64>() else {
                return Ok(None);
            };
            let number = i128::from(number);
            let (Some(next_sum), Some(next_sum_of_squares)) = (
                sum.checked_add(number),
                number.checked_mul(number).and_then(|square| sum_of_squares.checked_add(square)),
            ) else {
                return Ok(None);
            };
            sum = next_sum;
            sum_of_squares = next_sum_of_squares;
        }
        let count = i128::try_from(values.len()).expect("a row count fits");
        let numerator = count
            .checked_mul(sum_of_squares)
            .zip(sum.checked_mul(sum))
            .and_then(|(product, square)| product.checked_sub(square));
        let denominator = count.checked_mul(if self.sample { count - 1 } else { count });
        let (Some(numerator), Some(denominator)) = (numerator, denominator) else {
            return Ok(None);
        };
        // The exact quotient rounded once, as float(Fraction) does it.
        let variance: f64 =
            numerator.into_pyobject(py)?.call_method1(intern!(py, "__truediv__"), (denominator,))?.extract()?;
        let statistic = if self.is_deviation { variance.sqrt() } else { variance };
        Ok(Some(PyFloat::new(py, statistic).into_any()))
    }

    fn lock_values(&self) -> std::sync::MutexGuard<'_, Vec<Py<PyAny>>> {
        self.values.lock().unwrap_or_else(std::sync::PoisonError::into_inner)
    }
}

#[pymethods]
impl Statistic {
    #[new]
    fn new(sample: bool, is_deviation: bool, compute: Py<PyAny>, decimal_type: Py<PyAny>) -> Self {
        Statistic { sample, is_deviation, compute, decimal_type, values: Mutex::new(Vec::new()) }
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.compute)?;
        visit.call(&self.decimal_type)?;
        // The values are only ever changed with no Python code running, so they are never locked here.
        if let Ok(values) = self.values.try_lock() {
            for value in values.iter() {
                visit.call(value)?;
            }
        }
        Ok(())
    }

    /// Adds a row's value; NULL is skipped.
    fn step(&self, value: &Bound<'_, PyAny>) -> PyResult<()> {
        if value.is_none() {
            return Ok(());
        }
        let number = self.get_number(value)?.unbind();
        self.lock_values().push(number);
        Ok(())
    }

    /// Removes a row's value leaving the window frame - the first equal one.
    fn inverse(&self, value: &Bound<'_, PyAny>) -> PyResult<()> {
        if value.is_none() {
            return Ok(());
        }
        let py = value.py();
        let number = self.get_number(value)?;
        let values: Vec<Py<PyAny>> = self.lock_values().iter().map(|value| value.clone_ref(py)).collect();
        for (index, candidate) in values.iter().enumerate() {
            if candidate.bind(py).eq(&number)? {
                self.lock_values().remove(index);
                return Ok(());
            }
        }
        Err(pyo3::exceptions::PyValueError::new_err("list.remove(x): x not in list"))
    }

    /// The statistic of the values so far.
    fn value<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let values: Vec<Py<PyAny>> = self.lock_values().iter().map(|value| value.clone_ref(py)).collect();
        if let Some(statistic) = self.get_integer_statistic(py, &values)? {
            return Ok(statistic);
        }
        self.compute.bind(py).call1((PyList::new(py, values)?, self.sample, self.is_deviation))
    }

    /// The statistic of every value.
    fn finalize<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        self.value(py)
    }
}
