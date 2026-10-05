//! Writing a string of a format - an email address, a URL, a slug, an E.164 phone number: a str of
//! exactly that type the format accepts is normalized as the field normalizes it, checked by the
//! field's other validators and bound. Any other value - and a str the format check here doesn't
//! accept, valid or not - goes through the field's own `to_db_value`, which writes it or raises
//! the field's error.

use std::borrow::Cow;

use pyo3::prelude::*;
use pyo3::types::PyString;
use pyo3::{PyTraverseError, PyVisit};

use crate::codecs::email::EmailFormat;
use crate::codecs::inline_checks::InlineChecks;
use crate::codecs::options::Options;
use crate::codecs::url::UrlFormat;
use crate::codecs::{phone, slug};
use crate::python::references::PythonReferences;

/// The format of the field - `TextFormat` on the Python side.
enum TextFormat {
    Email(EmailFormat),
    Url(UrlFormat),
    Slug,
    Phone,
}

impl TextFormat {
    fn from_options(options: &Options<'_>) -> PyResult<Self> {
        Ok(match options.get_string("format")?.as_str() {
            "email" => TextFormat::Email(EmailFormat::new(options.get_bool("lowercase")?)),
            "url" => TextFormat::Url(UrlFormat::new(options.get_object("schemes")?.extract(options.py())?)),
            "slug" => TextFormat::Slug,
            "phone" => TextFormat::Phone,
            other => return Err(options.error(&format!("unknown text format {other:?}"))),
        })
    }

    /// The text as the field writes it, when the format accepts it.
    fn normalize<'text>(&self, text: &'text str) -> Option<Cow<'text, str>> {
        match self {
            TextFormat::Email(format) => format.normalize(text),
            TextFormat::Url(format) => format.is_valid(text).then_some(Cow::Borrowed(text)),
            TextFormat::Slug => slug::is_slug(text).then_some(Cow::Borrowed(text)),
            TextFormat::Phone => phone::is_e164_number(text).then_some(Cow::Borrowed(text)),
        }
    }
}

pub struct TextFormatWrite {
    format: TextFormat,
    checks: InlineChecks,
    /// `field.validate` when a validator other than the format's isn't inlined.
    validate: Option<Py<PyAny>>,
    null: bool,
    fallback: Py<PyAny>,
}

impl TextFormatWrite {
    pub fn from_options(options: &Options<'_>) -> PyResult<Self> {
        Ok(TextFormatWrite {
            format: TextFormat::from_options(options)?,
            checks: InlineChecks::from_options(options)?,
            validate: options.get_optional_object("validate")?,
            null: options.get_bool("null")?,
            fallback: options.get_object("fallback")?,
        })
    }

    pub fn write<'py>(
        &self,
        value: Bound<'py, PyAny>,
        instance: &Bound<'py, PyAny>,
        field_name: &Bound<'py, PyString>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        if value.is_none() && self.null {
            return Ok(value);
        }
        // A str that isn't valid UTF-8 (lone surrogates) goes to the field too. The inner None:
        // the text is written as it is.
        let normalized: Option<Option<String>> = value
            .cast_exact::<PyString>()
            .ok()
            .and_then(|text| text.to_str().ok())
            .and_then(|text| self.format.normalize(text))
            .map(|normalized| match normalized {
                Cow::Borrowed(_) => None,
                Cow::Owned(changed) => Some(changed),
            });
        let written = match normalized {
            None => return self.fallback.bind(py).call1((value, instance)),
            Some(None) => value,
            Some(Some(changed)) => PyString::new(py, &changed).into_any(),
        };
        match &self.validate {
            Some(validate) => {
                validate.bind(py).call1((&written,))?;
            }
            None => self.checks.check(&written, field_name)?,
        }
        Ok(written)
    }
}

impl PythonReferences for TextFormatWrite {
    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.validate)?;
        visit.call(&self.fallback)?;
        self.checks.traverse(visit)?;
        Ok(())
    }
}
