"""A field class brings its own lookups, value paths and value descriptions through Field hooks,
and a dialect adds what it brings to hare's own fields when registered - hare's core names no
dialect's field types."""

from functools import partial

import pytest

from hare.ddl.constraints import ExclusionConstraint
from hare.dialects.base.constants import SQL_DIALECT
from hare.dialects.base.sql_dialect import SqlDialect
from hare.dialects.dialect_registry import DialectRegistry
from hare.fields import CharField, IntField, TextField
from hare.query.enums import LookupValueShape
from hare.query.filters import FieldLookup, FieldLookups
from hare.query.filters.lookups.field_transforms import FieldTransforms
from hare.sql.terms import Term
from hare.sql.terms.functions import Function


class CodeField(CharField):
    """A code whose own lookups are equality and ``prefix``, and whose ``upper`` path reads it in
    upper case."""

    def get_lookups(self) -> dict[str, FieldLookup]:
        def starts_with(term: Term, value: str) -> Term:
            return term.like(f"{value}%")

        return {"": FieldLookup(lambda term, value: term == value), "prefix": FieldLookup(starts_with)}

    def get_path_transform(self, segment: str):
        if segment == "upper":
            return partial(Function, "UPPER"), self
        return None

    def get_lookup_value_description(self, lookup: str):
        if lookup == "prefix":
            return LookupValueShape.VALUE, str
        return None


class CountingDialect(SqlDialect):
    name = "lookup_hook_counting"
    install_count = 0

    def install(self) -> None:
        CountingDialect.install_count += 1


def test_a_field_brings_its_own_lookups():
    code_field = CodeField(max_length=8)
    code_field.model_field_name = "code"
    code_lookups = FieldLookups.get(code_field)
    # The field's own set replaces the generic one; register_lookup() lookups of CharField (other
    # test modules register some) are added to it.
    assert {"", "prefix"} <= set(code_lookups)
    assert "icontains" not in code_lookups
    assert code_field.get_lookup_value_description("prefix") == (LookupValueShape.VALUE, str)
    # The generic set, for a field without lookups of its own.
    assert "icontains" in FieldLookups.get(CharField(max_length=8))


def test_a_field_reads_inside_its_own_value():
    code_field = CodeField(max_length=8)
    transform = FieldTransforms.get_transform(code_field, "upper")
    assert transform is not None
    assert transform[1] is code_field
    assert FieldTransforms.get_transform(code_field, "lower") is None


def test_a_dialect_registers_a_path_segment_on_core_fields():
    had_own_transforms = "registered_transforms" in TextField.__dict__
    previous_transforms = dict(TextField.__dict__.get("registered_transforms", {}))
    TextField.register_transform(
        "reversed_text", lambda field: (partial(Function, "REVERSE"), field), required_extension="reverse_extension"
    )
    try:
        text_field = TextField()
        transform = FieldTransforms.get_transform(text_field, "reversed_text")
        assert transform is not None
        assert transform[1] is text_field
        assert FieldTransforms.get_transform(IntField(), "reversed_text") is None
        assert FieldTransforms.get_required_extension(text_field, "reversed_text") == "reverse_extension"
    finally:
        if had_own_transforms:
            TextField.registered_transforms = previous_transforms
        else:
            del TextField.registered_transforms


def test_postgresql_installs_unaccent_on_text_fields():
    DialectRegistry.load()
    assert FieldTransforms.get_required_extension(CharField(max_length=8), "unaccent") == "unaccent"
    assert FieldTransforms.get_transform(CharField(max_length=8), "unaccent") is not None
    assert FieldTransforms.get_transform(IntField(), "unaccent") is None


def test_a_dialect_is_installed_once_when_registered(monkeypatch):
    monkeypatch.setattr(DialectRegistry, "dialects_by_name", dict(DialectRegistry.dialects_by_name))
    monkeypatch.setattr(CountingDialect, "install_count", 0)
    dialect = CountingDialect()
    DialectRegistry.register_dialect(dialect)
    DialectRegistry.register_dialect(dialect)
    assert CountingDialect.install_count == 1


def test_copy_column_types_come_from_the_dialect():
    postgresql = DialectRegistry.get_dialect("postgresql")
    assert postgresql.parameters.supports_copy_column_type("text")
    assert not postgresql.parameters.supports_copy_column_type("CITEXT")
    assert not DialectRegistry.get_dialect("sqlite").parameters.supports_copy_column_type("TEXT")


def test_exclusion_constraint_extensions_come_from_the_dialect():
    constraint = ExclusionConstraint(name="c", expressions=(("team", "="),))
    assert (
        SQL_DIALECT.schema_editor_class.constraint_statements_class.get_exclusion_constraint_extension(
            constraint, {"team": IntField()}
        )
        is None
    )
    postgresql = DialectRegistry.get_dialect("postgresql")
    assert postgresql.schema_editor_class.constraint_statements_class.get_exclusion_constraint_extension(
        constraint, {"team": IntField()}
    ) == ("btree_gist")


@pytest.mark.parametrize("dialect_name", ["sqlite", "postgresql"])
def test_every_registered_driver_names_its_client_classes(dialect_name):
    drivers = [driver for driver in DialectRegistry.get_drivers() if driver.dialect.name == dialect_name]
    assert drivers
    assert any(driver.get_client_classes() for driver in drivers)
