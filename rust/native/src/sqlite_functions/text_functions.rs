//! The text functions `TextFunction` renders on SQLite, with Postgres's semantics: a negative
//! `LEFT`/`RIGHT` length drops characters from the other end, `SUBSTR` counts positions before 1 as
//! empty, `LPAD`/`RPAD` cut a longer text to the length. A text argument and whole-number lengths
//! are handled here, by code point as Python slices; anything else goes to the Python function.

use std::cmp::Ordering;
use std::fmt::Write;

use md5::Md5;
use pyo3::call::PyCallArgs;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyInt, PyString, PyTuple};
use pyo3::{PyTraverseError, PyVisit};
use sha1::Sha1;
use sha2::{Digest, Sha224, Sha256, Sha384, Sha512};

/// The characters of a str argument; None for anything else.
fn read_characters(value: &Bound<'_, PyAny>) -> Option<Vec<char>> {
    Some(value.cast::<PyString>().ok()?.to_str().ok()?.chars().collect())
}

/// A whole-number argument; None for anything else.
fn read_count(value: &Bound<'_, PyAny>) -> Option<i64> {
    if !value.is_instance_of::<PyInt>() {
        return None;
    }
    value.extract().ok()
}

/// `characters[start..end]` with Python's slice bounds - each clamped to the text.
fn slice(characters: &[char], start: i64, end: i64) -> String {
    let length = characters.len() as i64;
    let clamp = |index: i64| -> usize {
        let index = if index < 0 { (index + length).max(0) } else { index.min(length) };
        usize::try_from(index).unwrap_or(0)
    };
    let (start, end) = (clamp(start), clamp(end));
    if start >= end {
        return String::new();
    }
    characters[start..end].iter().collect()
}

/// The hex digest of a text's UTF-8 bytes.
fn get_digest<D: Digest>(text: &str) -> String {
    let digest = D::digest(text.as_bytes());
    digest.iter().fold(String::with_capacity(digest.len() * 2), |mut text, byte| {
        let _ = write!(text, "{byte:02x}");
        text
    })
}

/// The text functions; arguments they don't handle go to the Python ones given.
#[pyclass(frozen, module = "rust.native.sqlite_functions")]
pub struct TextFunctions {
    /// Name -> `(argument count, Python function)`, as `SqliteTextFunctions.get_functions()`.
    python_functions: Py<PyDict>,
}

impl TextFunctions {
    fn call_python<'py>(
        &self,
        py: Python<'py>,
        name: &str,
        arguments: impl PyCallArgs<'py>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let entry = self.python_functions.bind(py).get_item(name)?.expect("a registered text function");
        entry.get_item(1)?.call1(arguments)
    }

    fn text<'py>(py: Python<'py>, text: &str) -> Bound<'py, PyAny> {
        PyString::new(py, text).into_any()
    }

    fn pad<'py>(
        &self,
        name: &str,
        value: &Bound<'py, PyAny>,
        length: &Bound<'py, PyAny>,
        fill: &Bound<'py, PyAny>,
        on_left: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        if value.is_none() || length.is_none() || fill.is_none() {
            return Ok(py.None().into_bound(py));
        }
        let (Some(characters), Some(size), Some(fill_characters)) =
            (read_characters(value), read_count(length), read_characters(fill))
        else {
            return self.call_python(py, name, (value, length, fill));
        };
        if size <= 0 {
            return Ok(Self::text(py, ""));
        }
        let size = usize::try_from(size).unwrap_or(usize::MAX);
        if characters.len() >= size || fill_characters.is_empty() {
            return Ok(Self::text(py, &characters.iter().take(size).collect::<String>()));
        }
        let padding: String = fill_characters.iter().cycle().take(size - characters.len()).collect();
        let text: String = characters.iter().collect();
        Ok(Self::text(py, &if on_left { padding + &text } else { text + &padding }))
    }

    fn digest<'py>(
        &self,
        name: &str,
        value: &Bound<'py, PyAny>,
        digest: fn(&str) -> String,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        if value.is_none() {
            return Ok(py.None().into_bound(py));
        }
        match value.cast::<PyString>().ok().and_then(|text| text.to_str().ok()) {
            Some(text) => Ok(Self::text(py, &digest(text))),
            None => self.call_python(py, name, (value,)),
        }
    }
}

#[pymethods]
impl TextFunctions {
    #[new]
    fn new(python_functions: Py<PyDict>) -> Self {
        TextFunctions { python_functions }
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.python_functions)
    }

    fn left<'py>(&self, value: &Bound<'py, PyAny>, length: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        if value.is_none() || length.is_none() {
            return Ok(py.None().into_bound(py));
        }
        match (read_characters(value), read_count(length)) {
            (Some(characters), Some(count)) => Ok(Self::text(py, &slice(&characters, 0, count))),
            _ => self.call_python(py, "left", (value, length)),
        }
    }

    fn right<'py>(&self, value: &Bound<'py, PyAny>, length: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        if value.is_none() || length.is_none() {
            return Ok(py.None().into_bound(py));
        }
        let (Some(characters), Some(count)) = (read_characters(value), read_count(length)) else {
            return self.call_python(py, "right", (value, length));
        };
        let size = characters.len() as i64;
        let text = match count.cmp(&0) {
            Ordering::Greater => slice(&characters, (size - count).max(0), size),
            Ordering::Equal => String::new(),
            Ordering::Less => slice(&characters, -count, size),
        };
        Ok(Self::text(py, &text))
    }

    #[pyo3(signature = (*arguments))]
    fn substr<'py>(&self, arguments: &Bound<'py, PyTuple>) -> PyResult<Bound<'py, PyAny>> {
        let py = arguments.py();
        let length = arguments.len();
        if length == 2 || length == 3 {
            let value = arguments.get_item(0)?;
            let position = arguments.get_item(1)?;
            if value.is_none() || position.is_none() {
                return Ok(py.None().into_bound(py));
            }
            if let (Some(characters), Some(start)) = (read_characters(&value), read_count(&position)) {
                if length == 2 {
                    return Ok(Self::text(py, &slice(&characters, start.max(1) - 1, i64::MAX)));
                }
                let count_argument = arguments.get_item(2)?;
                if count_argument.is_none() {
                    return Ok(Self::text(py, &slice(&characters, start.max(1) - 1, i64::MAX)));
                }
                if let Some(count) = read_count(&count_argument).filter(|count| *count >= 0) {
                    let end = start.saturating_add(count);
                    return Ok(Self::text(py, &slice(&characters, start.max(1) - 1, (end - 1).max(0))));
                }
            }
        }
        self.call_python(py, "substr", arguments)
    }

    fn lpad<'py>(
        &self,
        value: &Bound<'py, PyAny>,
        length: &Bound<'py, PyAny>,
        fill: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        self.pad("lpad", value, length, fill, true)
    }

    fn rpad<'py>(
        &self,
        value: &Bound<'py, PyAny>,
        length: &Bound<'py, PyAny>,
        fill: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        self.pad("rpad", value, length, fill, false)
    }

    fn repeat<'py>(&self, value: &Bound<'py, PyAny>, count: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        if value.is_none() || count.is_none() {
            return Ok(py.None().into_bound(py));
        }
        match (value.cast::<PyString>().ok().and_then(|text| text.to_str().ok()), read_count(count)) {
            (Some(text), Some(times)) => Ok(Self::text(py, &text.repeat(usize::try_from(times.max(0)).unwrap_or(0)))),
            _ => self.call_python(py, "repeat", (value, count)),
        }
    }

    fn reverse<'py>(&self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        if value.is_none() {
            return Ok(py.None().into_bound(py));
        }
        match read_characters(value) {
            Some(characters) => Ok(Self::text(py, &characters.iter().rev().collect::<String>())),
            None => self.call_python(py, "reverse", (value,)),
        }
    }

    fn chr<'py>(&self, code: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = code.py();
        if code.is_none() {
            return Ok(py.None().into_bound(py));
        }
        let character = read_count(code)
            .filter(|number| *number != 0)
            .and_then(|number| u32::try_from(number).ok())
            .and_then(char::from_u32);
        match character {
            Some(character) => Ok(Self::text(py, &character.to_string())),
            None => self.call_python(py, "chr", (code,)),
        }
    }

    fn ascii<'py>(&self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        if value.is_none() {
            return Ok(py.None().into_bound(py));
        }
        match value.cast::<PyString>().ok().and_then(|text| text.to_str().ok()) {
            Some(text) => Ok(text.chars().next().map_or(0, u32::from).into_pyobject(py)?.into_any()),
            None => self.call_python(py, "ascii", (value,)),
        }
    }

    fn md5<'py>(&self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.digest("md5", value, get_digest::<Md5>)
    }

    fn sha1<'py>(&self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.digest("sha1", value, get_digest::<Sha1>)
    }

    fn sha224<'py>(&self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.digest("sha224", value, get_digest::<Sha224>)
    }

    fn sha256<'py>(&self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.digest("sha256", value, get_digest::<Sha256>)
    }

    fn sha384<'py>(&self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.digest("sha384", value, get_digest::<Sha384>)
    }

    fn sha512<'py>(&self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.digest("sha512", value, get_digest::<Sha512>)
    }
}
