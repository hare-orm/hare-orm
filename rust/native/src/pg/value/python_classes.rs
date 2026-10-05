//! The Python classes values convert to and from that have no C type of their own, imported once.

use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::PyType;

/// Python classes without a dedicated C-type, resolved by import once per interpreter.
pub(crate) static RANGE_TYPE: PyOnceLock<Py<PyType>> = PyOnceLock::new();
pub(crate) static IPV4_ADDRESS_TYPE: PyOnceLock<Py<PyType>> = PyOnceLock::new();
pub(crate) static IPV6_ADDRESS_TYPE: PyOnceLock<Py<PyType>> = PyOnceLock::new();
pub(crate) static IPV4_INTERFACE_TYPE: PyOnceLock<Py<PyType>> = PyOnceLock::new();
pub(crate) static IPV6_INTERFACE_TYPE: PyOnceLock<Py<PyType>> = PyOnceLock::new();
pub(crate) static IPV4_NETWORK_TYPE: PyOnceLock<Py<PyType>> = PyOnceLock::new();
pub(crate) static IPV6_NETWORK_TYPE: PyOnceLock<Py<PyType>> = PyOnceLock::new();

pub(crate) fn range_type(py: Python<'_>) -> PyResult<&Bound<'_, PyType>> {
    RANGE_TYPE.import(py, "hare.dialects.postgresql.fields.ranges", "Range")
}

/// True when `ob` is an `ipaddress` address, interface or network object.
pub(crate) fn is_ip_address_object(object: &Bound<'_, PyAny>) -> PyResult<bool> {
    let py = object.py();
    for class in [
        IPV4_ADDRESS_TYPE.import(py, "ipaddress", "IPv4Address")?,
        IPV6_ADDRESS_TYPE.import(py, "ipaddress", "IPv6Address")?,
        IPV4_NETWORK_TYPE.import(py, "ipaddress", "IPv4Network")?,
        IPV6_NETWORK_TYPE.import(py, "ipaddress", "IPv6Network")?,
    ] {
        if object.is_instance(class.as_any())? {
            return Ok(true);
        }
    }
    Ok(false)
}
