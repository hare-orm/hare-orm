"""The native constructor sets an instance up exactly as ``Model.__init__`` does - the same attributes
and values, the same errors - and leaves every other shape of arguments to it."""

from __future__ import annotations

import datetime
import inspect
from decimal import Decimal
from typing import Any

import pytest

from hare.fields.base.database_default import DatabaseDefault
from hare.models import Model
from hare.query.expressions import F
from hare.query.rows.hydrate_accelerator import HydrateAccelerator
from tests import testmodels

pytestmark = pytest.mark.skipif(HydrateAccelerator.module is None, reason="rust.native isn't built")


def construct(model: type[Model], kwargs: dict[str, Any], *, native: bool) -> Model | Exception:
    meta = model._meta
    if native:
        meta.instance_constructor = None
        assert meta.get_instance_constructor(), f"{model.__name__} has no native constructor"
    else:
        meta.instance_constructor = False
    try:
        return model(**kwargs)
    except Exception as error:  # noqa: BLE001 - the error is what is compared.
        return error
    finally:
        meta.instance_constructor = None


def assert_same(model: type[Model], kwargs: dict[str, Any]) -> None:
    native = construct(model, kwargs, native=True)
    reference = construct(model, kwargs, native=False)
    if isinstance(reference, Exception):
        assert type(native) is type(reference)
        assert str(native) == str(reference)
        return
    assert not isinstance(native, Exception), native
    assert native.__dict__.keys() == reference.__dict__.keys()
    fields_map = model._meta.fields_map
    for name, reference_value in reference.__dict__.items():
        native_value = native.__dict__[name]
        if isinstance(reference_value, Model):
            assert native_value is reference_value
        elif isinstance(reference_value, DatabaseDefault):
            assert type(native_value) is DatabaseDefault
            assert repr(native_value) == repr(reference_value)
        elif name in fields_map and callable(fields_map[name].default) and name not in kwargs:
            # A callable default gives every instance a value of its own.
            assert type(native_value) is type(reference_value), name
        else:
            assert native_value == reference_value, name
            assert type(native_value) is type(reference_value), name


def get_models() -> list[type[Model]]:
    return [
        model
        for model in vars(testmodels).values()
        if inspect.isclass(model)
        and issubclass(model, Model)
        and model is not Model
        and not model._meta.abstract
        and model._meta.app == "models"
    ]


@pytest.mark.asyncio
async def test_every_test_model_without_arguments(db):
    for model in get_models():
        if model._meta.get_instance_constructor():
            assert_same(model, {})


@pytest.mark.asyncio
async def test_plain_values(db):
    moment = datetime.datetime(2024, 3, 1, 12, 30, tzinfo=datetime.UTC)
    cases: list[tuple[type[Model], dict[str, Any]]] = [
        (testmodels.IntFields, {"intnum": 1}),
        (testmodels.IntFields, {"intnum": 1, "intnum_null": None}),
        (testmodels.IntFields, {"intnum": "7"}),
        (testmodels.IntFields, {"intnum": "seven"}),
        (testmodels.IntFields, {"intnum": None}),
        (testmodels.IntFields, {"intnum": True}),
        (testmodels.IntFields, {"id": 5, "intnum": 1}),
        (testmodels.IntFields, {"id": None, "intnum": 1}),
        (testmodels.IntFields, {"pk": 5, "intnum": 1}),
        (testmodels.IntFields, {"intnum": F("intnum") + 1}),
        (testmodels.IntFields, {"intnum": lambda: 3}),
        (testmodels.IntFields, {"missing": 1}),
        (testmodels.CharFields, {"char": "plain", "char_null": None}),
        (testmodels.CharFields, {"char": 123}),
        (testmodels.DecimalFields, {"decimal": Decimal("1.23456"), "decimal_nodec": 7}),
        (testmodels.DecimalFields, {"decimal": "bad", "decimal_nodec": 7}),
        (testmodels.DatetimeFields, {"datetime": moment}),
        (testmodels.DatetimeFields, {"datetime": moment.replace(tzinfo=None), "datetime_null": None}),
        (testmodels.DatetimeFields, {"datetime": "2024-03-01T12:30:00+02:00"}),
        (testmodels.JSONFields, {"data": {"a": [1, 2]}}),
        (testmodels.UUIDFields, {}),
    ]
    for model, kwargs in cases:
        assert_same(model, kwargs)


@pytest.mark.asyncio
async def test_relations(db):
    tournament = await testmodels.Tournament.objects.create(name="cup")
    reporter = await testmodels.Reporter.objects.create(name="ann")
    unsaved_tournament = testmodels.Tournament(name="draft")
    cases: list[dict[str, Any]] = [
        {"name": "final", "tournament": tournament},
        {"name": "final", "tournament": tournament, "reporter": reporter},
        {"name": "final", "tournament": tournament, "reporter": None},
        {"name": "final", "tournament_id": tournament.pk},
        {"name": "final", "tournament": tournament, "tournament_id": tournament.pk},
        {"name": "final", "tournament": tournament, "tournament_id": tournament.pk + 1},
        {"name": "final", "tournament": None},
        {"name": "final", "tournament": unsaved_tournament},
        {"name": "final", "tournament": reporter},
        {"name": "final", "tournament": tournament, "participants": []},
    ]
    for kwargs in cases:
        assert_same(testmodels.Event, kwargs)


@pytest.mark.asyncio
async def test_a_patched_to_python_is_used(db, monkeypatch):
    calls = []
    original_to_python = testmodels.CharFields._meta.fields_map["char"].__class__.to_python
    testmodels.CharFields._meta.instance_constructor = None
    testmodels.CharFields._meta.get_instance_constructor()

    def counting_to_python(self, value):
        calls.append(value)
        return original_to_python(self, value)

    monkeypatch.setattr(type(testmodels.CharFields._meta.fields_map["char"]), "to_python", counting_to_python)
    assert testmodels.CharFields(char=123).char == "123"
    assert calls == [123]
