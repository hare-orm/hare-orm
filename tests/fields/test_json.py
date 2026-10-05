from decimal import Decimal

import pytest
from pydantic import BaseModel

from hare.contrib.test import requires_features
from hare.ddl.indexes import Index
from hare.dialects.sqlite.functions.json import SqliteJsonEquality
from hare.exceptions import (
    ConfigurationError,
    DoesNotExist,
    IntegrityError,
    ValidationError,
)
from hare.fields import JSONField
from tests import testmodels


@pytest.mark.asyncio
async def test_empty(db):
    """Test that creating without required JSON field raises IntegrityError."""
    with pytest.raises(IntegrityError):
        await testmodels.JSONFields.objects.create()


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


def test_primary_key_with_explicit_unique_raises():
    with pytest.raises(ConfigurationError, match="can't be indexed"):
        JSONField(primary_key=True, unique=True)


class MyPydanticModel(BaseModel):
    name: str
    idx: Index
    model_config = dict(arbitrary_types_allowed=True)


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
    pyd_model = MyPydanticModel(name="", idx=Index(fields=["data"]))
    obj4 = await testmodels.JSONFields.objects.create(data="{}", data_index=pyd_model)
    obj4 = await testmodels.JSONFields.objects.get(id=obj4.id)
    assert obj4.data_index == {"idx": {"fields": ["data"]}, "name": ""}


@pytest.mark.asyncio
async def test_null_byte_in_value_raises_validation_error(db):
    """SQLite round-trips a null byte inside JSON/TEXT byte-for-byte, but Postgres's raw text
    protocol can't represent it in ANY text-based type and rejects it at INSERT time with an
    unrelated driver error - checked upfront here, uniformly on both dialects, instead of letting
    SQLite silently accept data that would only fail once deployed against Postgres."""
    with pytest.raises(ValidationError, match="null byte"):
        await testmodels.JSONFields.objects.create(data={"null_byte": "a\x00b"})
    with pytest.raises(ValidationError, match="null byte"):
        await testmodels.JSONFields.objects.create(data={"a\x00b": "value"})
    with pytest.raises(ValidationError, match="null byte"):
        await testmodels.JSONFields.objects.create(data=["a\x00b"])


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


@pytest.mark.asyncio
async def test_list_contains(db):
    """Test JSON contains filter on list."""
    await testmodels.JSONFields.objects.create(data=["text", 3, {"msg": "msg2"}])
    obj = await testmodels.JSONFields.objects.filter(data__contains=[{"msg": "msg2"}]).first()
    assert obj.data == ["text", 3, {"msg": "msg2"}]
    await obj.save()
    obj2 = await testmodels.JSONFields.objects.get(id=obj.id)
    assert obj == obj2


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


@pytest.mark.asyncio
async def test_filter_not_condition(db):
    """Test JSON filter with not condition."""
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
                        "name": "Fishy",
                    }
                ],
            },
        }
    )

    obj2 = await testmodels.JSONFields.objects.get(data__filter={"breed__not": "husky"})
    obj3 = await testmodels.JSONFields.objects.get(data__filter={"breed__not": "labrador"})
    assert obj0 == obj2
    assert obj1 == obj3


@pytest.mark.asyncio
async def test_filter_is_null_condition(db):
    """Test JSON filter with isnull condition."""
    obj0 = await testmodels.JSONFields.objects.create(
        data={
            "breed": "labrador",
            "owner": {
                "name": "Boby",
                "last": "Cloud",
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
            "breed": "labrador",
            "owner": {
                "name": None,
                "last": "Cloud",
                "other_pets": [
                    {
                        "name": "Fishy",
                    }
                ],
            },
        }
    )

    obj2 = await testmodels.JSONFields.objects.get(data__filter={"owner__name__isnull": False})
    obj3 = await testmodels.JSONFields.objects.get(data__filter={"owner__name__isnull": True})
    assert obj0 == obj2
    assert obj1 == obj3


@pytest.mark.asyncio
async def test_filter_not_is_null_condition(db):
    """Test JSON filter with not_isnull condition."""
    obj0 = await testmodels.JSONFields.objects.create(
        data={
            "breed": "labrador",
            "owner": {
                "name": "Boby",
                "last": "Cloud",
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
            "breed": "labrador",
            "owner": {
                "name": None,
                "last": "Cloud",
                "other_pets": [
                    {
                        "name": "Fishy",
                    }
                ],
            },
        }
    )

    obj2 = await testmodels.JSONFields.objects.get(data__filter={"owner__name__not_isnull": True})
    obj3 = await testmodels.JSONFields.objects.get(data__filter={"owner__name__not_isnull": False})
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


@pytest.mark.asyncio
async def test_contains_and_contained_by_follow_jsonb_containment(db):
    """Objects by key, arrays by element (order and repeats ignored), 1 equals 1.0 but not true,
    and only a top-level array contains a bare scalar - the jsonb @> rules, on every backend."""
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


@pytest.mark.asyncio
async def test_contains_and_has_key_do_not_match_a_sql_null_column(db):
    await testmodels.JSONFields.objects.create(data={"a": 1}, data_null=None)

    assert await testmodels.JSONFields.objects.filter(data_null__contains={}).count() == 0
    assert await testmodels.JSONFields.objects.filter(data_null__contained_by={"a": 1}).count() == 0
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


def test_unique_fail():
    """Test that JSONField cannot be unique."""
    with pytest.raises(ConfigurationError, match="can't be indexed"):
        JSONField(unique=True)


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


def test_to_python_value_invalid_utf8_bytes_raises_validation_error_not_unicode_decode_error():
    """from_db_value's except-branch built its error message via value.decode() (defaulting to
    utf-8) even for bytes that aren't valid JSON precisely because they aren't valid utf-8
    either - that decode call itself raised UnicodeDecodeError, masking the intended
    ValidationError with a completely different, uncaught exception type. Falls back to
    repr(value), which never fails on arbitrary bytes."""
    field = testmodels.JSONFields._meta.fields_map["data"]
    with pytest.raises(ValidationError, match=r"is invalid json value"):
        field.from_db_value(b"\xff\xfe not valid utf-8 or json")


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


def test_to_db_value_default_encoder_round_trips_a_str_value_as_a_string():
    """A str value is encoded as a JSON string literal, not decoded/re-encoded as whatever it
    happens to look like - reading it back (json.loads) gives back that same original string."""
    import json

    field = JSONField()
    result = field.to_db_value('{"b": 2, "a": 1}', None)
    assert json.loads(result) == '{"b": 2, "a": 1}'


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
async def test_data_pydantic_shape_mismatch_raises_validation_error_on_create(db):
    """JSONField(field_type=SomePydanticModel) used to validate the value's shape only on READ
    (from_db_value), not on WRITE (to_db_value) - a dict that doesn't match the pydantic
    model's schema used to be written successfully, only failing later, when some other read
    re-runs pydantic validation. Must now fail immediately at create() time."""
    with pytest.raises(ValidationError):
        await testmodels.JSONFields.objects.create(data=[], data_pydantic={"foo": "not an int", "bar": "baz"})

    with pytest.raises(ValidationError):
        await testmodels.JSONFields.objects.create(data=[], data_pydantic={"foo": 1})  # missing required "bar"


@pytest.mark.asyncio
async def test_data_pydantic_shape_mismatch_raises_validation_error_on_update(db):
    """Same shape validation as create() must also run on the QuerySet.update() write path."""
    obj = await testmodels.JSONFields.objects.create(data=[])
    with pytest.raises(ValidationError):
        await testmodels.JSONFields.objects.filter(pk=obj.pk).update(data_pydantic={"foo": "not an int", "bar": "baz"})


@pytest.mark.asyncio
async def test_data_pydantic_matching_shape_still_succeeds(db):
    """A plain dict matching the configured pydantic model's shape must still write and read
    back correctly - the new write-time validation must not reject valid data."""
    obj0 = await testmodels.JSONFields.objects.create(data=[], data_pydantic={"foo": 1, "bar": "baz"})
    obj = await testmodels.JSONFields.objects.get(id=obj0.id)
    assert obj.data_pydantic.foo == 1
    assert obj.data_pydantic.bar == "baz"


def test_to_db_value_pydantic_field_type_shape_mismatch_raises_immediately():
    """Direct to_db_value() call (no DB round trip) - the shape mismatch must be caught before
    any encoding/DB interaction happens at all, not merely by the time a full create() runs."""
    field = testmodels.JSONFields._meta.fields_map["data_pydantic"]
    with pytest.raises(ValidationError):
        field.to_db_value({"foo": "not an int", "bar": "baz"}, testmodels.JSONFields)


def test_to_python_value_pydantic_field_type_non_dict_raises_validation_error_not_typeerror():
    """The write path (to_db_value, tested above) checks the pydantic field_type's shape and
    raises a catchable ValidationError on a mismatch - the read path (from_db_value) had no
    equivalent guard: a stored value that decodes to valid JSON but isn't a dict (schema drift, a
    manual DB edit, or a row written before field_type was added to the field) reached a bare
    `self.field_type(**data)` call unguarded, raising a raw, uncatchable TypeError instead of the
    framework's own ValidationError every other conversion-failure path in this field raises."""
    field = testmodels.JSONFields._meta.fields_map["data_pydantic"]
    with pytest.raises(ValidationError):
        field.from_db_value("5")


@pytest.mark.asyncio
async def test_exact_and_not_compare_json_values_not_stored_text(db):
    """Key order and 1 vs 1.0 don't affect equality - same on SQLite as on Postgres jsonb."""
    first = await testmodels.JSONFields.objects.create(data={"a": 1, "b": 2})
    second = await testmodels.JSONFields.objects.create(data={"x": 1.0})
    third = await testmodels.JSONFields.objects.create(data="s")

    assert [row.id for row in await testmodels.JSONFields.objects.filter(data={"b": 2, "a": 1})] == [first.id]
    assert [row.id for row in await testmodels.JSONFields.objects.filter(data={"x": 1})] == [second.id]
    assert [row.id for row in await testmodels.JSONFields.objects.filter(data="s")] == [third.id]
    not_matching_ids = {row.id for row in await testmodels.JSONFields.objects.filter(data__not={"b": 2, "a": 1})}
    assert not_matching_ids == {second.id, third.id}


@pytest.mark.asyncio
async def test_in_and_not_in_compare_json_values_like_exact(db):
    """Key order and 1 vs 1.0 don't matter, None matches SQL NULL, same on every backend."""
    first = await testmodels.JSONFields.objects.create(data={"a": 1, "b": 2}, data_null={"a": 1})
    second = await testmodels.JSONFields.objects.create(data=[1, 2], data_null=None)
    third = await testmodels.JSONFields.objects.create(data="s", data_null="s")

    async def get_ids(**filters):
        return {row.id for row in await testmodels.JSONFields.objects.filter(**filters)}

    assert await get_ids(data__in=[{"b": 2, "a": 1}, [1.0, 2]]) == {first.id, second.id}
    assert await get_ids(data__in=["s"]) == {third.id}
    assert await get_ids(data__not_in=[{"b": 2, "a": 1}]) == {second.id, third.id}
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
async def test_declared_field_type_validates_and_converts_on_assign_write_and_read(db):
    """field_type=Model / list[Model]: a dict (or list of dicts) becomes the model in memory
    right away, the same value a later read returns; list items are validated too."""
    schema_type = testmodels.TestSchemaForJSONField
    row = await testmodels.JSONFieldsDeclaredType.objects.create(
        item={"foo": "1", "bar": "a"}, items=[schema_type(foo=2, bar="b"), {"foo": 3, "bar": "c"}]
    )

    assert row.item == schema_type(foo=1, bar="a")
    assert row.items == [schema_type(foo=2, bar="b"), schema_type(foo=3, bar="c")]
    fetched = await testmodels.JSONFieldsDeclaredType.objects.get(id=row.id)
    assert fetched.item == row.item
    assert fetched.items == row.items
    values = await testmodels.JSONFieldsDeclaredType.objects.filter(id=row.id).values("items")
    assert values == [{"items": row.items}]

    with pytest.raises(ValidationError):
        await testmodels.JSONFieldsDeclaredType.objects.create(items=[{"foo": "not an int", "bar": "x"}])
    with pytest.raises(ValidationError):
        await testmodels.JSONFieldsDeclaredType.objects.create(item={"bar": "missing foo"})


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


def test_sqlite_json_equality_canonicalizes_a_number_a_numeric_affinity_column_already_converted():
    """A column created earlier as JSON hands the equality UDF an int/float instead of text - it
    raised inside json.loads(), failing the whole query."""
    assert SqliteJsonEquality.canonicalize(1) == "1"
    assert SqliteJsonEquality.canonicalize(1.0) == "1"
    assert SqliteJsonEquality.canonicalize(1.5) == "1.5"


def test_native_storable_value_check_matches_the_python_walk(monkeypatch):
    """rust.hydrate walks a JSON value before it is written, instead of an isinstance chain per
    node in Python - it finds the same first problem the Python walk finds, and the same long
    integers."""
    from hare.fields.data.json import JsonCodec

    if JsonCodec.storable_value_checker is None:
        pytest.skip("this rust.hydrate build predates check_json_storable")
    field = JSONField()
    field.model_field_name = "payload"
    samples = [
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


class JsonDocument(BaseModel):
    title: str


def test_deconstruct_writes_field_type_only_when_declared():
    """The class's own value type of an undeclared document (dict[str, Any] | list[Any]) was
    written as field_type=..., a migration file then failing on the unimported typing.Any."""
    _, _, undeclared_kwargs = JSONField().deconstruct()
    _, _, declared_kwargs = JSONField(field_type=JsonDocument).deconstruct()

    assert "field_type" not in undeclared_kwargs
    assert declared_kwargs["field_type"] is JsonDocument
