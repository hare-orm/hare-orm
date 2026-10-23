"""A filter on a literal annotation took a value of another type as it was: asyncpg refused the parameter
with an OperationalError, rust_pg and ClickHouse cast it on the server and matched, SQLite matched
nothing. The value goes through the literal's own field, as a field's value does."""

import pytest

from hare.exceptions import ValidationError
from hare.query.expressions import Value
from tests.shared_models import testmodels


@pytest.mark.asyncio
async def test_a_value_of_another_type_than_the_literal_is_refused_on_every_driver(db):
    await testmodels.IntFields.objects.create(intnum=1)
    annotated = testmodels.IntFields.objects.annotate(five=Value(5))
    assert await annotated.filter(five=5).count() == 1
    assert await annotated.filter(five__in=[4, 5]).count() == 1
    for refused in (lambda: annotated.filter(five="5").count(), lambda: annotated.filter(five__in=["5"]).count()):
        with pytest.raises(ValidationError, match="five: expected an int"):
            await refused()
