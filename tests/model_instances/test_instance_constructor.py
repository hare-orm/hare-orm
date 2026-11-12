"""The constructor of a model's instances - one native implementation assigning keywords and defaults,
handing the special cases to the Python functions of ``InstanceInitialization``."""

import pytest

from hare.exceptions import FieldError, QueryError, ValidationError
from hare.models.instances.instance_initialization import InstanceInitialization
from hare.native.native_modules import NativeModules
from hare.query.expressions import F
from tests.shared_models.testmodels import Event, IntFields, Team, Tournament


def test_the_constructor_is_native_and_kept_per_model(db):
    constructor = InstanceInitialization.get_constructor(IntFields)
    assert isinstance(constructor, NativeModules.rows.InstanceConstructor)
    assert InstanceInitialization.get_constructor(IntFields) is constructor


def test_keywords_are_assigned_and_left_out_fields_get_their_defaults(db):
    obj = IntFields(intnum=5)
    assert obj.intnum == 5
    assert obj.intnum_null is None
    assert obj.id is None
    assert obj._saved_in_db is False


def test_a_value_of_another_type_goes_through_the_fields_to_python(db):
    with pytest.raises(ValidationError):
        IntFields(intnum="5")
    assert Team(id=1, name="team").name == "team"


def test_none_for_a_field_that_doesnt_take_it_is_refused_before_any_assignment(db):
    with pytest.raises(QueryError, match="doesn't take None"):
        IntFields(intnum=None)


def test_an_expression_is_kept_for_the_write():
    expression = F("intnum") + 1
    assert IntFields(intnum=expression).intnum is expression


def test_a_callable_value_is_called_now(db):
    assert IntFields(intnum=lambda: 7).intnum == 7


def test_pk_is_spread_over_the_primary_key(db):
    assert IntFields(pk=3, intnum=1).id == 3
    with pytest.raises(QueryError, match="name the same field"):
        IntFields(pk=3, id=3, intnum=1)


def test_an_automatic_primary_key_given_none_is_left_to_its_generated_value(db):
    obj = IntFields(id=None, intnum=1)
    assert obj.id is None
    assert not obj._custom_generated_pk


def test_an_unknown_or_unsettable_keyword_is_refused(db):
    with pytest.raises(FieldError, match="Unknown field"):
        IntFields(intnum=1, missing=2)
    with pytest.raises(QueryError, match="many-to-many"):
        Event(name="e", participants=[])


@pytest.mark.asyncio
async def test_a_relation_and_its_key_column_must_agree(db):
    tournament = await Tournament.objects.create(name="t")
    event = Event(name="e", tournament=tournament)
    assert event.tournament is tournament
    assert event.tournament_id == tournament.id
    with pytest.raises(QueryError, match="Conflicting values"):
        Event(name="e", tournament=tournament, tournament_id=tournament.id + 1)
    with pytest.raises(QueryError, match="first call .save"):
        Event(name="e", tournament=Tournament(name="unsaved"))


def test_update_from_dict_converts_through_the_same_constructor(db):
    obj = IntFields(intnum=1)
    obj.update_from_dict({"intnum": 9, "unknown": 1})
    assert obj.intnum == 9
    with pytest.raises(QueryError, match="doesn't take None"):
        obj.update_from_dict({"intnum": None})


def test_an_automatic_primary_key_left_out_is_set_past_setattr_and_counts_as_generated(db):
    obj = IntFields(intnum=1)
    assert obj.__dict__["id"] is None
    assert "_custom_generated_pk" not in obj.__dict__
    assert not obj._custom_generated_pk
    obj.id = 9
    assert obj._custom_generated_pk
