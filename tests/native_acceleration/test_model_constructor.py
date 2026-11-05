"""Construction preserves values, defaults, relation consistency and field overrides."""

import datetime
from decimal import Decimal

import pytest

from hare.exceptions import FieldError, QueryError, ValidationError
from hare.query.expressions import F
from tests.shared_models import testmodels, testmodels_postgres


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"intnum": 1}, {"intnum": 1, "intnum_null": None}),
        ({"intnum": 1, "intnum_null": None}, {"intnum": 1, "intnum_null": None}),
        ({"id": 5, "intnum": 1}, {"id": 5, "intnum": 1}),
        ({"id": None, "intnum": 1}, {"id": None, "intnum": 1}),
        ({"pk": 5, "intnum": 1}, {"id": 5, "intnum": 1}),
        ({"intnum": lambda: 3}, {"intnum": 3}),
    ],
)
@pytest.mark.asyncio
async def test_constructor_assigns_scalar_values(db, kwargs, expected):
    obj = testmodels.IntFields(**kwargs)
    assert not obj._saved_in_db
    for name, value in expected.items():
        assert getattr(obj, name) == value
        assert type(getattr(obj, name)) is type(value)


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"intnum": "7"}, ValidationError),
        ({"intnum": "seven"}, ValidationError),
        ({"intnum": True}, ValidationError),
        ({"intnum": None}, QueryError),
        ({"missing": 1}, FieldError),
    ],
)
@pytest.mark.asyncio
async def test_constructor_refuses_invalid_scalar_arguments(db, kwargs, error):
    with pytest.raises(error):
        testmodels.IntFields(**kwargs)


@pytest.mark.asyncio
async def test_constructor_preserves_expressions_and_typed_values(db):
    expression = F("intnum") + 1
    assert testmodels.IntFields(intnum=expression).intnum is expression
    assert testmodels.CharFields(char="plain", char_null=None).char == "plain"
    with pytest.raises(ValidationError):
        testmodels.CharFields(char=123)
    moment = datetime.datetime(2024, 3, 1, 12, 30, tzinfo=datetime.UTC)
    assert testmodels.DatetimeFields(datetime=moment).datetime == moment
    assert testmodels.DatetimeFields(datetime="2024-03-01T12:30:00+02:00").datetime == datetime.datetime(
        2024, 3, 1, 12, 30, tzinfo=datetime.timezone(datetime.timedelta(hours=2))
    )
    assert testmodels.JSONFields(data={"a": [1, 2]}).data == {"a": [1, 2]}
    first_json, second_json = testmodels.JSONFields(), testmodels.JSONFields()
    first_json.data_default["a"] = 2
    assert second_json.data_default == {"a": 1}
    first, second = testmodels.UUIDFields(), testmodels.UUIDFields()
    assert first.pk != second.pk
    assert not first._saved_in_db and not second._saved_in_db


@pytest.mark.postgresql_models
@pytest.mark.asyncio
async def test_constructor_converts_decimal_exchange_values(db):
    obj = testmodels_postgres.DecimalFields(decimal=Decimal("1.23456"), decimal_nodec=7)
    assert obj.decimal == Decimal("1.2346")
    assert obj.decimal_nodec == Decimal(7)
    assert type(obj.decimal_nodec) is Decimal
    with pytest.raises(ValidationError):
        testmodels_postgres.DecimalFields(decimal="bad", decimal_nodec=7)


@pytest.mark.asyncio
async def test_constructor_assigns_relations_and_checks_their_keys(db):
    tournament = await testmodels.Tournament.objects.create(name="cup")
    reporter = await testmodels.Reporter.objects.create(name="ann")
    for extra in ({}, {"reporter": reporter}, {"reporter": None}, {"tournament_id": tournament.pk}):
        event = testmodels.Event(name="final", tournament=tournament, **extra)
        assert event.tournament is tournament
        assert event.tournament_id == tournament.pk
        if "reporter" in extra:
            assert event.reporter is extra["reporter"]
    event = testmodels.Event(name="final", tournament_id=tournament.pk)
    assert event.tournament_id == tournament.pk
    for kwargs, error in (
        ({"tournament": tournament, "tournament_id": tournament.pk + 1}, QueryError),
        ({"tournament": None}, QueryError),
        ({"tournament": testmodels.Tournament(name="draft")}, QueryError),
        ({"tournament": reporter}, ValidationError),
        ({"tournament": tournament, "participants": []}, QueryError),
    ):
        with pytest.raises(error):
            testmodels.Event(name="final", **kwargs)


@pytest.mark.asyncio
async def test_a_patched_to_python_is_used(db, monkeypatch):
    calls = []
    original_to_python = testmodels.CharFields._meta.fields_map["char"].__class__.to_python

    def counting_to_python(self, value):
        calls.append(value)
        return original_to_python(self, value)

    monkeypatch.setattr(type(testmodels.CharFields._meta.fields_map["char"]), "to_python", counting_to_python)
    with pytest.raises(ValidationError, match="expected a str"):
        testmodels.CharFields(char=123)
    assert calls == [123]
