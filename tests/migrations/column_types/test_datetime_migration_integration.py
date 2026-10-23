"""Integration test for DatetimeField migration generation bug fix.

This test ensures that models with auto_now and auto_now_add fields
generate valid migrations that can be applied without ConfigurationError.
"""

import importlib

import pytest

from hare import Model, fields

pytestmark = pytest.mark.database_independent


class TimestampedModel(Model):
    """Model with both created_at and modified_at fields."""

    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=100)
    created_at = fields.DatetimeField(auto_now_add=True)
    modified_at = fields.DatetimeField(auto_now=True)

    class Meta:
        app = "test_app"


@pytest.mark.parametrize(
    ("field_name", "flag", "other_flag"),
    [
        pytest.param("modified_at", "auto_now", "auto_now_add", id="auto_now_field_deconstructs_with_only_auto_now"),
        pytest.param(
            "created_at", "auto_now_add", "auto_now", id="auto_now_add_field_deconstructs_with_only_auto_now_add"
        ),
    ],
)
def test_an_automatic_field_deconstructs_with_only_its_flag(field_name, flag, other_flag):
    """An automatic field is written to a migration with its own flag and without the other."""
    _path, _args, kwargs = TimestampedModel._meta.fields_map[field_name].deconstruct()

    assert kwargs[flag] is True
    assert other_flag not in kwargs


def test_regular_field_deconstructs_without_either_flag():
    _path, _args, kwargs = TimestampedModel._meta.fields_map["name"].deconstruct()

    assert "auto_now" not in kwargs
    assert "auto_now_add" not in kwargs


def test_field_rebuilt_from_its_deconstruction_keeps_the_flags():
    """A field rebuilt from what a migration writes of it is valid and keeps both flags."""
    for name, auto_now, auto_now_add in (("modified_at", True, False), ("created_at", False, True)):
        path, args, kwargs = TimestampedModel._meta.fields_map[name].deconstruct()
        module_name, class_name = path.rsplit(".", 1)
        rebuilt = getattr(importlib.import_module(module_name), class_name)(*args, **kwargs)

        assert rebuilt.auto_now is auto_now
        assert rebuilt.auto_now_add is auto_now_add
