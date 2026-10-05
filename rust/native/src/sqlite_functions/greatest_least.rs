//! Postgres's `GREATEST`/`LEAST` on SQLite: the greatest or least argument that isn't NULL (SQLite's
//! own multi-argument `max()`/`min()` give NULL when any argument is), numbers - a `DecimalField`'s
//! text included - compared as numbers; arguments of mixed types go to the Python function.

use std::cmp::Ordering;

use pyo3::prelude::*;
use pyo3::types::{PyFloat, PyInt, PyString, PyTuple};
use pyo3::{PyTraverseError, PyVisit};

use crate::sqlite_functions::decimal_number::DecimalNumber;

/// What every argument is compared by - all of one type, or the Python function decides.
enum SortKeys {
    Integers(Vec<i128>),
    Floats(Vec<f64>),
    Texts(Vec<String>),
    /// Exact decimals - integers and decimal text.
    Numbers(Vec<Vec<u8>>),
}

/// The byte key of an exact decimal.
fn get_number_key(number: &DecimalNumber) -> Vec<u8> {
    let mut key = Vec::new();
    number.write_key(&mut key);
    key
}

/// The sort keys of the arguments; None when they are of mixed types this doesn't compare exactly.
fn get_sort_keys(values: &[Bound<'_, PyAny>], compares_numbers: bool) -> PyResult<Option<SortKeys>> {
    if values.iter().all(PyAnyMethods::is_instance_of::<PyFloat>) {
        return Ok(Some(SortKeys::Floats(values.iter().map(PyAnyMethods::extract).collect::<PyResult<_>>()?)));
    }
    if values.iter().any(PyAnyMethods::is_instance_of::<PyFloat>) {
        return Ok(None);
    }
    if values.iter().all(PyAnyMethods::is_instance_of::<PyInt>) {
        let integers: Option<Vec<i128>> = values.iter().map(|value| value.extract().ok()).collect();
        return Ok(integers.map(SortKeys::Integers));
    }
    if compares_numbers {
        let mut keys = Vec::with_capacity(values.len());
        for value in values {
            let number = if value.is_instance_of::<PyInt>() {
                let Ok(integer) = value.extract::<i128>() else {
                    return Ok(None);
                };
                DecimalNumber::from_digits(integer < 0, integer.unsigned_abs().to_string().into_bytes(), 0)
            } else if let Ok(text) = value.cast::<PyString>() {
                let Some(number) = DecimalNumber::parse(text.to_str()?) else {
                    return Ok(None);
                };
                number
            } else {
                return Ok(None);
            };
            keys.push(get_number_key(&number));
        }
        return Ok(Some(SortKeys::Numbers(keys)));
    }
    if values.iter().all(PyAnyMethods::is_instance_of::<PyString>) {
        let texts: PyResult<Vec<String>> =
            values.iter().map(|value| Ok(value.cast::<PyString>()?.to_str()?.to_owned())).collect();
        return Ok(Some(SortKeys::Texts(texts?)));
    }
    Ok(None)
}

/// The index of the first greatest (`wanted` `Greater`) or least (`Less`) key.
fn pick_index<T>(keys: &[T], compare: impl Fn(&T, &T) -> Option<Ordering>, wanted: Ordering) -> Option<usize> {
    let mut picked = 0;
    for index in 1..keys.len() {
        match compare(&keys[index], &keys[picked]) {
            Some(ordering) if ordering == wanted => picked = index,
            Some(_) => {}
            // NaN - Python's max()/min() keep their own answer for it.
            None => return None,
        }
    }
    Some(picked)
}

/// The `GREATEST`/`LEAST` functions; arguments they don't compare themselves go to the Python
/// function given.
#[pyclass(frozen, module = "rust.native.sqlite_functions")]
pub struct GreatestLeast {
    /// `(is_greatest, comparison_type, *values)`.
    pick_in_python: Py<PyAny>,
}

impl GreatestLeast {
    fn pick<'py>(
        &self,
        wanted: Ordering,
        comparison_type: &Bound<'py, PyAny>,
        values: &Bound<'py, PyTuple>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = values.py();
        let present: Vec<Bound<'py, PyAny>> = values.iter().filter(|value| !value.is_none()).collect();
        if present.is_empty() {
            return Ok(py.None().into_bound(py));
        }
        let compares_numbers =
            comparison_type.cast::<PyString>().is_ok_and(|text| text.to_str().ok() == Some("number"));
        if comparison_type.is_instance_of::<PyString>() {
            let index = match get_sort_keys(&present, compares_numbers)? {
                Some(SortKeys::Integers(keys)) => pick_index(&keys, |left, right| Some(left.cmp(right)), wanted),
                Some(SortKeys::Floats(keys)) => pick_index(&keys, PartialOrd::partial_cmp, wanted),
                Some(SortKeys::Texts(keys)) => pick_index(&keys, |left, right| Some(left.cmp(right)), wanted),
                Some(SortKeys::Numbers(keys)) => pick_index(&keys, |left, right| Some(left.cmp(right)), wanted),
                None => None,
            };
            if let Some(index) = index {
                return Ok(present[index].clone());
            }
        }
        let mut arguments =
            vec![(wanted == Ordering::Greater).into_pyobject(py)?.to_owned().into_any(), comparison_type.clone()];
        arguments.extend(values.iter());
        self.pick_in_python.bind(py).call1(PyTuple::new(py, arguments)?)
    }
}

#[pymethods]
impl GreatestLeast {
    #[new]
    fn new(pick_in_python: Py<PyAny>) -> Self {
        GreatestLeast { pick_in_python }
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.pick_in_python)
    }

    /// The greatest argument that isn't NULL, NULL when every one is.
    #[pyo3(signature = (comparison_type, *values))]
    fn greatest<'py>(
        &self,
        comparison_type: &Bound<'py, PyAny>,
        values: &Bound<'py, PyTuple>,
    ) -> PyResult<Bound<'py, PyAny>> {
        self.pick(Ordering::Greater, comparison_type, values)
    }

    /// The least argument that isn't NULL, NULL when every one is.
    #[pyo3(signature = (comparison_type, *values))]
    fn least<'py>(
        &self,
        comparison_type: &Bound<'py, PyAny>,
        values: &Bound<'py, PyTuple>,
    ) -> PyResult<Bound<'py, PyAny>> {
        self.pick(Ordering::Less, comparison_type, values)
    }
}
