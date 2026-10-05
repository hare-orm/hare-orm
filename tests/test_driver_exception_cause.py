"""A translated database error carries the driver's own exception as its __cause__."""

import pytest

from hare.exceptions import IntegrityError, OperationalError
from tests.testmodels import Tournament


@pytest.mark.asyncio
async def test_integrity_error_is_raised_from_the_driver_exception(db):
    await Tournament.objects.create(id=1, name="first")
    with pytest.raises(IntegrityError) as raised:
        await Tournament.objects.create(id=1, name="duplicate")
    cause = raised.value.__cause__
    assert cause is not None
    assert cause is raised.value.args[0]
    if Tournament._meta.connection.dialect.name == "postgresql":
        assert cause.sqlstate == "23505"
        assert cause.table_name == "tournament"


@pytest.mark.asyncio
async def test_operational_error_is_raised_from_the_driver_exception(db):
    with pytest.raises(OperationalError) as raised:
        await Tournament._meta.connection.execute("SELECT missing_column FROM tournament")
    assert raised.value.__cause__ is not None
    if Tournament._meta.connection.dialect.name == "postgresql":
        assert raised.value.__cause__.sqlstate == "42703"
