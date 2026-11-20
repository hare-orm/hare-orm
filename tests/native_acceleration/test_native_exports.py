"""Native declarations and callbacks retain an importable identity during pickling."""

import importlib
import pickle
import sys
from types import BuiltinFunctionType

import pytest

from hare import fields

pytestmark = pytest.mark.database_independent


@pytest.mark.parametrize("module_name", ["rows", "clock", "sqlite_functions", "pg", "pool"])
def test_native_functions_can_be_imported_and_pickled_by_their_qualified_module(module_name):
    if module_name in {"pg", "pool"} and sys.platform == "win32":
        pytest.skip("The native PostgreSQL driver is built on Unix")
    module = importlib.import_module(f"rust.native.{module_name}")
    exports = [
        value
        for name, value in vars(module).items()
        if not name.startswith("_") and isinstance(value, BuiltinFunctionType)
    ]
    assert exports
    for value in exports:
        assert pickle.loads(pickle.dumps(value)) is value
        assert value.__module__ == module.__name__
    assert getattr(importlib.import_module("rust.native"), module_name) is module


@pytest.mark.parametrize("field_class", [fields.JSONField, fields.JSONTextField])
@pytest.mark.parametrize("nested", [False, True])
def test_field_pickle_preserves_native_json_callbacks_and_independent_defaults(field_class, nested):
    declaration = field_class(default={"labels": ["first"]})
    if nested:
        declaration = fields.ArrayField(declaration, default=[{"labels": ["first"]}])
    copied = pickle.loads(pickle.dumps(declaration))
    assert type(copied) is type(declaration)
    assert copied.default == declaration.default
    original_json = declaration.base_field if nested else declaration
    copied_json = copied.base_field if nested else copied
    assert copied_json.decoder is original_json.decoder
    assert copied_json.encoder is original_json.encoder
    copied_json.default["labels"].append("second")
    assert original_json.default == {"labels": ["first"]}
