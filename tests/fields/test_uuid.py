import uuid
import warnings

import pytest

from hare import fields
from hare.exceptions import IntegrityError, ValidationError
from hare.fields.database_default import DatabaseDefault
from hare.fields.db_defaults import SqlDefault
from hare.warnings import RedundantDbDefaultWarning
from tests import testmodels


def test_primary_key_with_db_default_does_not_auto_inject_client_default():
    """UUIDField(primary_key=True) auto-injects default=uuid4 when the caller gave no other way
    to generate the value - but it used to only check "default" not in kwargs, so a
    db_default=SqlDefault("gen_random_uuid()") (asking Postgres to generate it server-side
    instead) got the client-side uuid4 forced in anyway, permanently defeating the db_default a
    caller explicitly asked for, with a misleading warning blaming a conflict the caller never
    created."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", RedundantDbDefaultWarning)
        field = fields.UUIDField(primary_key=True, db_default=SqlDefault("gen_random_uuid()"))
    assert field.default is None
    assert isinstance(field.get_db_default_value(), DatabaseDefault)


def test_primary_key_without_default_or_db_default_still_auto_injects_uuid4():
    """Backward compatibility: the common case (neither default= nor db_default= given) still
    gets a client-side uuid4 so the pk is always populated in memory before save()."""
    field = fields.UUIDField(primary_key=True)
    assert field.default is uuid.uuid4


@pytest.mark.asyncio
async def test_empty(db):
    with pytest.raises(IntegrityError):
        await testmodels.UUIDFields.objects.create()


@pytest.mark.asyncio
async def test_create(db):
    data = uuid.uuid4()
    obj0 = await testmodels.UUIDFields.objects.create(data=data)
    assert isinstance(obj0.data, uuid.UUID)
    assert isinstance(obj0.data_auto, uuid.UUID)
    assert obj0.data_null is None
    obj = await testmodels.UUIDFields.objects.get(id=obj0.id)
    assert isinstance(obj.data, uuid.UUID)
    assert isinstance(obj.data_auto, uuid.UUID)
    assert obj.data == data
    assert obj.data_null is None
    await obj.save()
    obj2 = await testmodels.UUIDFields.objects.get(id=obj.id)
    assert obj == obj2

    await obj.delete()
    obj = await testmodels.UUIDFields.objects.filter(id=obj0.id).first()
    assert obj is None


@pytest.mark.asyncio
async def test_update(db):
    data = uuid.uuid4()
    data2 = uuid.uuid4()
    obj0 = await testmodels.UUIDFields.objects.create(data=data)
    await testmodels.UUIDFields.objects.filter(id=obj0.id).update(data=data2)
    obj = await testmodels.UUIDFields.objects.get(id=obj0.id)
    assert obj.data == data2
    assert obj.data_null is None


@pytest.mark.asyncio
async def test_create_not_null(db):
    data = uuid.uuid4()
    obj0 = await testmodels.UUIDFields.objects.create(data=data, data_null=data)
    obj = await testmodels.UUIDFields.objects.get(id=obj0.id)
    assert obj.data == data
    assert obj.data_null == data


def test_to_db_value_calls_validate():
    """UUIDField.to_db_value used to be the only field type that never called self.validate() -
    custom validators=[...] silently never fired on write."""
    from hare import fields
    from hare.exceptions import ValidationError
    from hare.fields.validators import Validator

    class AlwaysFails(Validator):
        def __call__(self, value) -> None:
            raise ValidationError("always fails")

    field = fields.UUIDField(validators=[AlwaysFails()])
    with pytest.raises(ValidationError):
        field.to_db_value(uuid.uuid4(), None)


@pytest.mark.asyncio
async def test_plain_attribute_assignment_rejects_malformed_string(db):
    """A malformed string set via plain attribute assignment (not construction) used to be
    stringified as-is with no validation, and only fail on a later read - it must fail at
    save() instead."""
    from hare.exceptions import ValidationError

    obj = await testmodels.UUIDFields.objects.create(data=uuid.uuid4())
    obj.data = "not-a-uuid-at-all"
    with pytest.raises(ValidationError):
        await obj.save()


def test_to_db_value_rejects_empty_string_instead_of_silently_nulling():
    """to_db_value() used `if not value: return None` - a falsy-but-not-None value (an empty
    string, from a plain attribute assignment bypassing from_db_value) was silently converted
    to NULL instead of being rejected, even on a non-nullable field. It's only caught today by
    the DB's own NOT NULL constraint rejecting the resulting NULL - a nullable UUIDField would
    have silently accepted "" as if it meant "no value", instead of raising the same malformed-
    UUID error any other garbage string gets."""
    from hare.exceptions import ValidationError

    field = testmodels.UUIDFields._meta.fields_map["data"]
    with pytest.raises(ValidationError, match="badly formed hexadecimal UUID string"):
        field.to_db_value("", testmodels.UUIDFields)


def test_to_python_value_wraps_malformed_string():
    """from_db_value() - the path a value read straight from the DB goes through - called
    UUID(value) unwrapped, raising a bare ValueError for malformed input (e.g. garbage written
    outside the ORM) instead of the framework's own catchable ValidationError every other field
    validation failure raises."""
    from hare.exceptions import ValidationError

    field = testmodels.UUIDFields._meta.fields_map["data"]
    with pytest.raises(ValidationError, match="badly formed hexadecimal UUID string"):
        field.from_db_value("not-a-uuid-at-all")


@pytest.mark.parametrize("bad_value", [123, 1.5, [1], b"not-a-uuid"])
def test_non_string_value_raises_validation_error(bad_value):
    field = testmodels.UUIDFields._meta.fields_map["data"]
    with pytest.raises(ValidationError, match="expected a UUID or UUID string"):
        field.to_db_value(bad_value, testmodels.UUIDFields)
    with pytest.raises(ValidationError, match="expected a UUID or UUID string"):
        field.from_db_value(bad_value)
