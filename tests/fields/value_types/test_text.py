import pytest

from hare.exceptions import ValidationError
from hare.fields import TextField
from hare.transactions.transactions import Transactions
from tests.shared_models import testmodels


@pytest.mark.database_independent
def test_index_and_unique_accepted():
    assert TextField(db_index=True).index
    assert TextField(unique=True).unique


@pytest.mark.database_independent
def test_primary_key_accepted():
    assert TextField(primary_key=True).pk


@pytest.mark.asyncio
async def test_a_null_byte_is_the_databases_to_store(db):
    """SQLite keeps a null byte in text; PostgreSQL refuses it - a ValidationError, as for a refused
    value, not an OperationalError: on a write, an update and a filter alike."""
    value = "a\x00b"
    if testmodels.TextFields._meta.connection.dialect.name != "postgresql":
        created = await testmodels.TextFields.objects.create(text=value)
        assert (await testmodels.TextFields.objects.get(id=created.id)).text == value
        return
    created = await testmodels.TextFields.objects.create(text="plain")
    for refused in (
        lambda: testmodels.TextFields.objects.create(text=value),
        lambda: testmodels.TextFields.objects.filter(id=created.id).update(text=value),
        lambda: testmodels.TextFields.objects.filter(text=value).count(),
    ):
        with pytest.raises(ValidationError, match="null byte"):
            async with Transactions.atomic():
                await refused()
    assert await testmodels.TextFields.objects.values_list("text", flat=True) == ["plain"]
