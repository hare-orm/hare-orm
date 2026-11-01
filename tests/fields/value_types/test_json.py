import enum
from decimal import Decimal
from typing import Any

import pytest

from hare.contrib.test import assert_query_count, requires_features
from hare.ddl.indexes import Index
from hare.exceptions import (
    ConfigurationError,
    DoesNotExist,
    UnSupportedError,
    ValidationError,
)
from hare.fields import JSONField
from hare.query.functions import Max, Min
from hare.transactions.transactions import Transactions
from tests.shared_models import testmodels


@pytest.mark.database_independent
def test_primary_key_does_not_force_unique_and_index():
    """Field.__init__'s `if primary_key: db_index = True; unique = True` ran BEFORE the
    `not self.indexable and (unique or db_index)` guard - JSONField declares indexable=False, so
    primary_key=True used to silently reach that promotion anyway (the guard only ever saw the
    caller's OWN unique/db_index arguments, never what primary_key implies), ending up as a
    genuinely unique+indexed PK despite indexable=False. It must neither raise (an explicit
    unique=/db_index= still correctly does, see test_primary_key_with_explicit_unique_raises
    below) nor silently set unique/index true - the PRIMARY KEY constraint itself, driven by
    .pk, already provides whatever DB-level uniqueness the PK needs."""
    field = JSONField(primary_key=True)
    assert field.pk is True
    assert field.unique is False
    assert field.index is False


@pytest.mark.database_independent
def test_primary_key_with_explicit_unique_raises():
    with pytest.raises(ConfigurationError, match="can't be indexed"):
        JSONField(primary_key=True, unique=True)


@pytest.mark.asyncio
async def test_create(db):
    """Test JSON field creation and retrieval."""
    obj0 = await testmodels.JSONFields.objects.create(data={"some": ["text", 3]})
    obj = await testmodels.JSONFields.objects.get(id=obj0.id)
    assert obj.data == {"some": ["text", 3]}
    assert obj.data_null is None
    await obj.save()
    obj2 = await testmodels.JSONFields.objects.get(id=obj.id)
    assert obj == obj2
    obj3 = await testmodels.JSONFields.objects.create(data="{}", data_decimal=Decimal(0))
    obj3 = await testmodels.JSONFields.objects.get(id=obj3.id)
    assert str(obj3.data_decimal) == "0"


@pytest.mark.asyncio
async def test_a_null_byte_in_a_value_is_the_databases_to_store(db):
    """SQLite stores a null byte in JSON as it is; PostgreSQL refuses it itself - a ValidationError, as
    for a refused value, not an OperationalError."""
    dialect_name = testmodels.JSONFields._meta.connection.dialect.name
    for data in ({"null_byte": "a\x00b"}, {"a\x00b": "value"}, ["a\x00b"]):
        if dialect_name == "postgresql":
            with pytest.raises(ValidationError, match="null byte"):
                async with Transactions.atomic():
                    await testmodels.JSONFields.objects.create(data=data)
            continue
        created = await testmodels.JSONFields.objects.create(data=data)
        assert (await testmodels.JSONFields.objects.get(id=created.id)).data == data


@pytest.mark.asyncio
async def test_error(db):
    """A plain string is stored as-is (even one that isn't valid JSON syntax, like '{"some": ')
    - JSONField no longer interprets a str/bytes value as JSON source text to parse, see
    test_string_value_round_trips_as_a_plain_string below. Only a value the encoder genuinely
    can't serialize still raises ValidationError."""
    # A value the encoder can't serialize (e.g. Decimal, an Index instance) used to raise a bare
    # TypeError straight from the encoder call - now wrapped as ValidationError, matching every
    # other field's own conversion-failure convention (see hare/fields/data/json.py).
    with pytest.raises(ValidationError):
        await testmodels.JSONFields.objects.create(data=Decimal(0))

    with pytest.raises(ValidationError):
        await testmodels.JSONFields.objects.create(data_decimal=Index(fields=["data"]))


@pytest.mark.asyncio
async def test_update(db):
    """Test JSON field update."""
    obj0 = await testmodels.JSONFields.objects.create(data={"some": ["text", 3]})
    await testmodels.JSONFields.objects.filter(id=obj0.id).update(data={"other": ["text", 5]})
    obj = await testmodels.JSONFields.objects.get(id=obj0.id)
    assert obj.data == {"other": ["text", 5]}
    assert obj.data_null is None


@pytest.mark.asyncio
async def test_dict_update_does_not_parse_a_json_looking_string(db):
    """A str value passed to .update() is stored and read back as that same plain string, not
    parsed into the dict it happens to look like - a JSON-object-shaped string is just a string,
    same as any other."""
    obj0 = await testmodels.JSONFields.objects.create(data={"some": ["text", 3]})

    obj = await testmodels.JSONFields.objects.get(id=obj0.id)
    assert obj.data == {"some": ["text", 3]}

    await testmodels.JSONFields.objects.filter(id=obj0.id).update(data='{"other": ["text", 5]}')
    obj = await testmodels.JSONFields.objects.get(id=obj0.id)
    assert obj.data == '{"other": ["text", 5]}'


@pytest.mark.asyncio
async def test_list_update_does_not_parse_a_json_looking_string(db):
    """Same as test_dict_update_does_not_parse_a_json_looking_string, for a list-shaped string."""
    obj = await testmodels.JSONFields.objects.create(data="anything")
    obj0 = await testmodels.JSONFields.objects.get(id=obj.id)
    assert obj0.data == "anything"

    await testmodels.JSONFields.objects.filter(id=obj.id).update(data='["text", 5]')
    obj0 = await testmodels.JSONFields.objects.get(id=obj.id)
    assert obj0.data == '["text", 5]'


@pytest.mark.asyncio
async def test_string_value_round_trips_as_a_plain_string(db):
    """Whatever comes in gets serialized and stored as-is, same as a number/dict/list - a bare
    string is no exception, whether or not it happens to also be valid JSON syntax."""
    obj0 = await testmodels.JSONFields.objects.create(data="hello")
    obj = await testmodels.JSONFields.objects.get(id=obj0.id)
    assert obj.data == "hello"

    obj1 = await testmodels.JSONFields.objects.create(data='{"some": ')  # not even valid JSON syntax
    obj = await testmodels.JSONFields.objects.get(id=obj1.id)
    assert obj.data == '{"some": '

    obj.data = "error json"
    await obj.save()
    obj = await testmodels.JSONFields.objects.get(id=obj1.id)
    assert obj.data == "error json"


@pytest.mark.asyncio
async def test_list(db):
    """Test JSON field with list data."""
    obj0 = await testmodels.JSONFields.objects.create(data=["text", 3])
    obj = await testmodels.JSONFields.objects.get(id=obj0.id)
    assert obj.data == ["text", 3]
    assert obj.data_null is None
    await obj.save()
    obj2 = await testmodels.JSONFields.objects.get(id=obj.id)
    assert obj == obj2


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_list_contains(db):
    """Test JSON contains filter on list."""
    await testmodels.JSONFields.objects.create(data=["text", 3, {"msg": "msg2"}])
    obj = await testmodels.JSONFields.objects.filter(data__contains=[{"msg": "msg2"}]).first()
    assert obj.data == ["text", 3, {"msg": "msg2"}]
    await obj.save()
    obj2 = await testmodels.JSONFields.objects.get(id=obj.id)
    assert obj == obj2


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_list_contained_by(db):
    """Test JSON contained_by filter on list."""
    obj0 = await testmodels.JSONFields.objects.create(data=["text"])
    obj1 = await testmodels.JSONFields.objects.create(data=["hare", "msg"])
    obj2 = await testmodels.JSONFields.objects.create(data=["hare"])
    obj3 = await testmodels.JSONFields.objects.create(data=["new_message", "some_message"])
    objs = set(await testmodels.JSONFields.objects.filter(data__contained_by=["text", "hare", "msg"]))
    created_objs = {obj0, obj1, obj2}
    assert created_objs == objs
    assert obj3 not in objs


@pytest.mark.asyncio
async def test_filter(db):
    """Test JSON filter with nested data."""
    obj0 = await testmodels.JSONFields.objects.create(
        data={
            "breed": "labrador",
            "owner": {
                "name": "Bob",
                "last": None,
                "other_pets": [
                    {
                        "name": "Fishy",
                    }
                ],
            },
        }
    )
    obj1 = await testmodels.JSONFields.objects.create(
        data={
            "breed": "husky",
            "owner": {
                "name": "Goldast",
                "last": None,
                "other_pets": [
                    {
                        "name": None,
                    }
                ],
            },
        }
    )
    obj = await testmodels.JSONFields.objects.get(data__filter={"breed": "labrador"})
    obj2 = await testmodels.JSONFields.objects.get(data__filter={"owner__name": "Goldast"})
    obj3 = await testmodels.JSONFields.objects.get(data__filter={"owner__other_pets__0__name": "Fishy"})

    assert obj0 == obj
    assert obj1 == obj2
    assert obj0 == obj3

    with pytest.raises(DoesNotExist):
        await testmodels.JSONFields.objects.get(data__filter={"breed": "NotFound"})
    with pytest.raises(DoesNotExist):
        await testmodels.JSONFields.objects.get(data__filter={"owner__other_pets__0__name": "NotFound"})


def make_dog(breed, owner_name, owner_last):
    return {
        "breed": breed,
        "owner": {"name": owner_name, "last": owner_last, "other_pets": [{"name": "Fishy"}]},
    }


@pytest.mark.parametrize(
    ("first_data", "second_data", "first_filter", "second_filter"),
    [
        pytest.param(
            make_dog("labrador", "Bob", None),
            make_dog("husky", "Goldast", None),
            {"breed__not": "husky"},
            {"breed__not": "labrador"},
            id="filter_not_condition",
        ),
        pytest.param(
            make_dog("labrador", "Boby", "Cloud"),
            make_dog("labrador", None, "Cloud"),
            {"owner__name__isnull": False},
            {"owner__name__isnull": True},
            id="filter_is_null_condition",
        ),
        pytest.param(
            make_dog("labrador", "Boby", "Cloud"),
            make_dog("labrador", None, "Cloud"),
            {"owner__name__not_isnull": True},
            {"owner__name__not_isnull": False},
            id="filter_not_is_null_condition",
        ),
    ],
)
@pytest.mark.asyncio
async def test_filter_negated_condition(db, first_data, second_data, first_filter, second_filter):
    """JSON filter with a not, isnull or not_isnull condition picks each of two rows."""
    obj0 = await testmodels.JSONFields.objects.create(data=first_data)
    obj1 = await testmodels.JSONFields.objects.create(data=second_data)

    obj2 = await testmodels.JSONFields.objects.get(data__filter=first_filter)
    obj3 = await testmodels.JSONFields.objects.get(data__filter=second_filter)
    assert obj0 == obj2
    assert obj1 == obj3


@pytest.mark.asyncio
async def test_nested_json_path_isnull_rejects_non_bool_value(db):
    """The nested `data__filter={"...__isnull": ...}` path (hare/dialects/postgresql/lookups/json.py)
    dispatches straight to is_null()/not_null() with the raw dict value, bypassing bool_encoder
    entirely - is_null()/not_null() must reject a non-bool value themselves."""
    from hare.exceptions import UnSupportedError

    await testmodels.JSONFields.objects.create(data={"owner": {"name": "Boby"}})

    with pytest.raises(UnSupportedError):
        await testmodels.JSONFields.objects.get(data__filter={"owner__name__isnull": "false"})
    with pytest.raises(UnSupportedError):
        await testmodels.JSONFields.objects.get(data__filter={"owner__name__not_isnull": "false"})


@pytest.mark.asyncio
async def test_field_level_isnull(db):
    """`data_null__isnull` filters on the JSON COLUMN itself being NULL (not a nested JSON-path
    key, see test_filter_is_null_condition above for that) - a plain column IS NULL check that
    works identically on sqlite and postgres."""
    obj0 = await testmodels.JSONFields.objects.create(data={"a": 1}, data_null={"b": 2})
    obj1 = await testmodels.JSONFields.objects.create(data={"a": 1})

    assert set(await testmodels.JSONFields.objects.filter(data_null__isnull=True).values_list("id", flat=True)) == {
        obj1.id
    }
    assert set(await testmodels.JSONFields.objects.filter(data_null__isnull=False).values_list("id", flat=True)) == {
        obj0.id
    }
    assert set(
        await testmodels.JSONFields.objects.filter(data_null__not_isnull=True).values_list("id", flat=True)
    ) == {obj0.id}
    assert set(
        await testmodels.JSONFields.objects.filter(data_null__not_isnull=False).values_list("id", flat=True)
    ) == {obj1.id}


@pytest.mark.asyncio
async def test_field_level_isnull_rejects_non_bool_value(db):
    """bool_encoder used to coerce via bare bool(value) - a truthy non-bool like "false" used to
    silently invert the filter instead of raising."""
    from hare.exceptions import UnSupportedError

    with pytest.raises(UnSupportedError):
        await testmodels.JSONFields.objects.filter(data_null__isnull="false").values_list("id", flat=True)
    with pytest.raises(UnSupportedError):
        await testmodels.JSONFields.objects.filter(data_null__not_isnull="false").values_list("id", flat=True)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_contains_and_contained_by_follow_jsonb_containment(db):
    """Objects by key, arrays by element (order and repeats ignored), 1 equals 1.0 but not true,
    and only a top-level array contains a bare scalar - the jsonb @> rules."""
    cases = [
        ({"a": [1, 2]}, {"a": 1}, False),
        ([1, [2, 3]], [[3]], True),
        (["foo", "bar"], "bar", True),
        (1, 1.0, True),
        ([1, 2], [], True),
        ({}, [], False),
        ([True], [1], False),
        ([1, 1], [1], True),
        ([{"a": 1, "b": 2}], [{"a": 1}], True),
        ([[1, 2]], [1], False),
        (["a"], ["a", "a"], True),
        ({"a": ["x"]}, {"a": "x"}, False),
        ([["x"]], ["x"], False),
        ({"a": {"b": 1, "c": 2}}, {"a": {"b": 1}}, True),
    ]
    for container, contained, expected in cases:
        row = await testmodels.JSONFields.objects.create(data=container, data_null=contained)
        contains_match = await testmodels.JSONFields.objects.filter(id=row.id, data__contains=contained).exists()
        contained_by_match = await testmodels.JSONFields.objects.filter(
            id=row.id, data_null__contained_by=container
        ).exists()
        assert (contains_match, contained_by_match) == (expected, expected), (container, contained)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_contains_does_not_match_a_sql_null_column(db):
    await testmodels.JSONFields.objects.create(data={"a": 1}, data_null=None)

    assert await testmodels.JSONFields.objects.filter(data_null__contains={}).count() == 0
    assert await testmodels.JSONFields.objects.filter(data_null__contained_by={"a": 1}).count() == 0


@requires_features(dialect="sqlite")
@pytest.mark.parametrize("lookup", ["contains", "contained_by"])
@pytest.mark.asyncio
async def test_json_containment_is_refused_where_the_database_has_none(db, lookup):
    async with assert_query_count(0):
        with pytest.raises(UnSupportedError, match=f"JSONField __{lookup} needs a JSON containment"):
            await testmodels.JSONFields.objects.filter(**{f"data__{lookup}": {"a": 1}}).count()


@pytest.mark.asyncio
async def test_has_key_does_not_match_a_sql_null_column(db):
    await testmodels.JSONFields.objects.create(data={"a": 1}, data_null=None)

    assert await testmodels.JSONFields.objects.filter(data_null__has_key="a").count() == 0
    assert await testmodels.JSONFields.objects.filter(data_null__has_keys=[]).count() == 0


@pytest.mark.asyncio
async def test_values(db):
    """Test JSON field in values()."""
    obj0 = await testmodels.JSONFields.objects.create(data={"some": ["text", 3]})
    values = await testmodels.JSONFields.objects.filter(id=obj0.id).values("data")
    assert values[0]["data"] == {"some": ["text", 3]}


@pytest.mark.asyncio
async def test_values_list(db):
    """Test JSON field in values_list()."""
    obj0 = await testmodels.JSONFields.objects.create(data={"some": ["text", 3]})
    values = await testmodels.JSONFields.objects.filter(id=obj0.id).values_list("data", flat=True)
    assert values[0] == {"some": ["text", 3]}


@pytest.mark.database_independent
def test_unique_fail():
    """Test that JSONField cannot be unique."""
    with pytest.raises(ConfigurationError, match="can't be indexed"):
        JSONField(unique=True)


@pytest.mark.database_independent
def test_index_fail():
    """Test that JSONField cannot be indexed."""
    with pytest.raises(ConfigurationError, match="can't be indexed"):
        JSONField(db_index=True)


@pytest.mark.asyncio
async def test_validate_str_rejected_by_dict_or_list_validator(db):
    """A str value is no longer parsed into the dict/list it looks like - raise_if_not_dict_or_list
    now correctly sees the plain string it actually is and rejects it, same as it would reject
    any other non-dict/list value."""
    with pytest.raises(ValidationError, match="must be a dict or list"):
        await testmodels.JSONFields.objects.create(data=[], data_validate='["text", 5]')


@pytest.mark.asyncio
async def test_validate_dict(db):
    """Test JSON field with validate from dict."""
    obj0 = await testmodels.JSONFields.objects.create(data=[], data_validate={"some": ["text", 3]})
    obj = await testmodels.JSONFields.objects.get(id=obj0.id)
    assert obj.data_validate == {"some": ["text", 3]}


@pytest.mark.asyncio
async def test_validate_list(db):
    """Test JSON field with validate from list."""
    obj0 = await testmodels.JSONFields.objects.create(data=[], data_validate=["text", 3])
    obj = await testmodels.JSONFields.objects.get(id=obj0.id)
    assert obj.data_validate == ["text", 3]


@pytest.mark.database_independent
def test_to_db_value_wraps_encoder_failure():
    """JSONField.to_db_value's final `self.encoder(value)` call (for a dict/list value not
    already str/bytes/a pydantic model) had no error handling at all - a value containing
    something the encoder can't serialize (e.g. a plain custom-class instance nested inside a
    dict) raised a bare TypeError instead of the framework's own ValidationError, matching every
    other field's own conversion-failure convention (Int/CharEnumFieldInstance/UUIDField/
    DecimalField)."""

    class Unserializable:
        pass

    field = testmodels.JSONFields._meta.fields_map["data"]
    with pytest.raises(ValidationError):
        field.to_db_value({"a": Unserializable()}, testmodels.JSONFields)


@pytest.mark.database_independent
def test_to_db_value_runs_custom_encoder_on_str_input():
    """A str/bytes value runs through `self.encoder()` exactly as given - a custom encoder (e.g.
    one that encrypts) sees the same raw value regardless of its Python type."""
    calls = []

    def recording_encoder(value):
        calls.append(value)
        return "ENCRYPTED"

    field = JSONField(encoder=recording_encoder)

    assert field.to_db_value('{"a": 1}', None) == "ENCRYPTED"
    assert calls == ['{"a": 1}']

    calls.clear()
    assert field.to_db_value(b'{"a": 1}', None) == "ENCRYPTED"
    assert calls == [b'{"a": 1}']

    calls.clear()
    assert field.to_db_value({"a": 1}, None) == "ENCRYPTED"
    assert calls == [{"a": 1}]


@pytest.mark.database_independent
def test_to_db_value_default_encoder_round_trips_a_str_value_as_a_string():
    """A str value is encoded as a JSON string literal, not decoded/re-encoded as whatever it
    happens to look like - reading it back (json.loads) gives back that same original string."""
    import json

    field = JSONField()
    result = field.to_db_value('{"b": 2, "a": 1}', None)
    assert json.loads(result) == '{"b": 2, "a": 1}'


@pytest.mark.database_independent
def test_json_field_error_matches_other_fields_validation_error_convention():
    """JSONField used to be the one field type raising FieldError (instead of ValidationError)
    on a conversion failure, breaking calling code written to catch `(ValueError,
    ValidationError)` per this codebase's own established convention (see Field.to_db_value's
    own try/except, and Int/CharEnumFieldInstance/UUIDField/DecimalField). A genuine encode
    failure (write) and a genuine decode failure of real, stored DB text (read) must both raise
    ValidationError like every other field - a plain str written via to_db_value is no longer
    one of those failure cases (see test_string_value_round_trips_as_a_plain_string)."""
    field = testmodels.JSONFields._meta.fields_map["data"]

    class Unserializable:
        pass

    with pytest.raises(ValidationError):
        field.to_db_value({"a": Unserializable()}, testmodels.JSONFields)

    with pytest.raises(ValidationError):
        field.from_db_value("not valid json")


@pytest.mark.asyncio
async def test_exact_and_not_compare_json_values(db):
    """A JSON value equals the same value written the same way; whether key order or a number's
    spelling matter is the database's own - PostgreSQL's jsonb compares values, SQLite the text."""
    first = await testmodels.JSONFields.objects.create(data={"a": 1, "b": 2})
    second = await testmodels.JSONFields.objects.create(data={"x": 1.5})
    third = await testmodels.JSONFields.objects.create(data="s")

    assert [row.id for row in await testmodels.JSONFields.objects.filter(data={"a": 1, "b": 2})] == [first.id]
    assert [row.id for row in await testmodels.JSONFields.objects.filter(data={"x": 1.5})] == [second.id]
    assert [row.id for row in await testmodels.JSONFields.objects.filter(data="s")] == [third.id]
    not_matching_ids = {row.id for row in await testmodels.JSONFields.objects.filter(data__not={"a": 1, "b": 2})}
    assert not_matching_ids == {second.id, third.id}
    reordered = [row.id for row in await testmodels.JSONFields.objects.filter(data={"b": 2, "a": 1})]
    is_sqlite = testmodels.JSONFields._meta.connection.dialect.name == "sqlite"
    assert reordered == ([] if is_sqlite else [first.id])


@pytest.mark.asyncio
async def test_in_and_not_in_compare_json_values_like_exact(db):
    """A list holds JSON values compared as ``=`` compares them; None matches SQL NULL."""
    first = await testmodels.JSONFields.objects.create(data={"a": 1, "b": 2}, data_null={"a": 1})
    second = await testmodels.JSONFields.objects.create(data=[1, 2], data_null=None)
    third = await testmodels.JSONFields.objects.create(data="s", data_null="s")

    async def get_ids(**filters):
        return {row.id for row in await testmodels.JSONFields.objects.filter(**filters)}

    assert await get_ids(data__in=[{"a": 1, "b": 2}, [1, 2]]) == {first.id, second.id}
    assert await get_ids(data__in=["s"]) == {third.id}
    assert await get_ids(data__not_in=[{"a": 1, "b": 2}]) == {second.id, third.id}
    assert await get_ids(data__in=[]) == set()
    assert await get_ids(data__not_in=[]) == {first.id, second.id, third.id}
    assert await get_ids(data_null__in=[None, "s"]) == {second.id, third.id}
    assert await get_ids(data_null__not_in=["s"]) == {first.id, second.id}
    assert await get_ids(data_null__not_in=[None, "s"]) == {first.id}
    assert await get_ids(data__in=testmodels.JSONFields.objects.filter(id=first.id).values("data")) == {first.id}


@pytest.mark.asyncio
async def test_in_and_not_in_with_a_long_json_value_list(db):
    first = await testmodels.JSONFields.objects.create(data={"k": 7})
    second = await testmodels.JSONFields.objects.create(data=[1, 2])
    long_values = [{"k": number} for number in range(1500)]

    assert {row.id for row in await testmodels.JSONFields.objects.filter(data__in=long_values)} == {first.id}
    assert {row.id for row in await testmodels.JSONFields.objects.filter(data__not_in=long_values)} == {second.id}


@pytest.mark.asyncio
async def test_top_level_numbers_round_trip_exactly(db):
    """SQLite declared the column as JSON - NUMERIC affinity turned a stored top-level number into
    INTEGER/REAL: digits past float precision were lost and 1.0 came back as 1."""
    big = await testmodels.JSONFields.objects.create(data=12345678901234567890)
    whole_float = await testmodels.JSONFields.objects.create(data=1.0)

    assert (await testmodels.JSONFields.objects.get(id=big.id)).data == 12345678901234567890
    whole_float_value = (await testmodels.JSONFields.objects.get(id=whole_float.id)).data
    assert (whole_float_value, type(whole_float_value)) == (1.0, float)
    assert await testmodels.JSONFields.objects.filter(data=12345678901234567890).count() == 1


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_sqlite_json_column_has_text_affinity(db):
    await testmodels.JSONFields.objects.create(data=1.0)
    connection = testmodels.JSONFields._meta.connection
    rows = await connection.execute_dicts('SELECT typeof("data") AS storage_class FROM "jsonfields"')
    assert {row["storage_class"] for row in rows} == {"text"}


@pytest.mark.database_independent
def test_native_storable_value_check_matches_the_python_walk(monkeypatch):
    """rust.hydrate walks a JSON value before it is written, instead of an isinstance chain per
    node in Python - it finds the same first problem the Python walk finds, and the same long
    integers."""
    from hare.fields.data.json.json_codec import JsonCodec

    if JsonCodec.storable_value_checker is None:
        pytest.skip("this rust.hydrate build predates check_json_storable")
    field = JSONField()
    field.model_field_name = "payload"
    circular_list: list[Any] = [1]
    circular_list.append(circular_list)
    circular_dict: dict[str, Any] = {}
    circular_dict["self"] = {"inner": [circular_dict]}
    shared = ["shared"]
    samples = [
        circular_list,
        circular_dict,
        {"twice": [shared, shared], "again": shared},
        {"tags": ["a", "b"], "nested": {"deep": [1, 2.5, None, True]}},
        {"big": 2**64, "small": -(2**63) - 1},
        {"edge": [2**64 - 1, -(2**63)]},
        ("tuple", ["x", {"k": "v"}]),
        {"bad key\x00": 1},
        ["fine", float("nan")],
        [float("inf"), "null\x00byte"],
        ["null\x00byte", float("-inf")],
        {"a": float("nan"), "b": "x\x00"},
    ]
    for sample in samples:
        outcomes = []
        for checker in (JsonCodec.storable_value_checker, None):
            monkeypatch.setattr(JsonCodec, "storable_value_checker", checker)
            try:
                outcomes.append(field.check_storable_value(sample))
            except ValidationError as error:
                outcomes.append(str(error))
        monkeypatch.undo()
        assert outcomes[0] == outcomes[1], sample


@pytest.mark.database_independent
def test_deconstruct_writes_no_field_type_when_undeclared():
    """The class's own value type of an undeclared document (dict[str, Any] | list[Any]) was
    written as field_type=..., a migration file then failing on the unimported typing.Any."""
    _, _, undeclared_kwargs = JSONField().deconstruct()

    assert "field_type" not in undeclared_kwargs


@pytest.mark.asyncio
async def test_min_and_max_of_json_are_the_databases(db):
    """SQLite takes the least and the greatest stored text; PostgreSQL has no min()/max() of jsonb - refused
    before any SQL."""
    await testmodels.JSONFields.objects.create(data={"a": 1})
    await testmodels.JSONFields.objects.create(data={"b": 2})
    if testmodels.JSONFields._meta.connection.dialect.name == "sqlite":
        assert await testmodels.JSONFields.objects.aggregate(value=Max("data")) == {"value": {"b": 2}}
        assert await testmodels.JSONFields.objects.aggregate(value=Min("data")) == {"value": {"a": 1}}
        return
    with pytest.raises(UnSupportedError, match="JSONField - the postgresql database has no MAX"):
        await testmodels.JSONFields.objects.aggregate(value=Max("data"))


class JsonFloat(float):
    pass


class JsonKey(str):
    pass


class JsonColor(str, enum.Enum):
    RED = "red"


@pytest.mark.asyncio
async def test_subclasses_of_json_types_are_stored_as_their_base_values(db):
    """orjson writes the subclasses of int, str, dict and list, not a float subclass's value nor a str
    subclass as a key (a StrEnum member) - JSONField refused them as not JSON serializable."""
    for data, stored in (
        ({"ratio": JsonFloat(1.5)}, {"ratio": 1.5}),
        ([JsonFloat(2.5), 3], [2.5, 3]),
        ({JsonKey("key"): 1}, {"key": 1}),
        ({JsonColor.RED: 1}, {"red": 1}),
    ):
        created = await testmodels.JSONFields.objects.create(data=data)
        assert (await testmodels.JSONFields.objects.get(id=created.id)).data == stored


@pytest.mark.database_independent
def test_a_declared_type_without_pydantic_names_its_extra(monkeypatch):
    """A field_type declared without pydantic installed left the field's values unvalidated."""
    from hare.lazy_loading.pydantic_classes import PydanticClasses

    monkeypatch.setattr(PydanticClasses, "loaded", True)
    monkeypatch.setattr(PydanticClasses, "type_adapter", None)
    field = JSONField(field_type=list[str])
    field.model_field_name = "tags"
    with pytest.raises(ConfigurationError, match=r"hare-orm\[pydantic\]"):
        field.to_db_value(["a"], None)
