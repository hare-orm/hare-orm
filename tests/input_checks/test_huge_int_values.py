"""An int of more digits than Python writes as text raised a bare ValueError: from a message showing it
(a slice's bound, limit(), Round()'s precision, atomic(retries=), iterator(chunk_size=)) and from its
binding (Value(), a function's argument, F() + int on PostgreSQL and ClickHouse)."""

import pytest

from hare.exceptions import OperationalError, QueryError, ValidationError
from hare.query.expressions import F, Value
from hare.query.functions import Left, Round
from hare.transactions.transactions import Transactions
from tests.shared_models import testmodels

HUGE = 10**5000


@pytest.mark.asyncio
async def test_a_count_or_option_too_long_to_show_is_refused_with_its_message(db):
    queryset = testmodels.IntFields.objects.all()
    for refused in (
        lambda: queryset[:HUGE],
        lambda: queryset[HUGE : HUGE + 1],
        lambda: queryset.limit(HUGE),
        lambda: queryset.offset(HUGE),
    ):
        with pytest.raises(QueryError, match="digits"):
            refused()
    with pytest.raises(QueryError, match="digits"):
        await queryset.iterator(chunk_size=HUGE).__anext__()
    with pytest.raises(QueryError, match="digits"):
        async with Transactions.atomic(retries=HUGE):
            pass
    with pytest.raises(QueryError, match="digits"):
        await queryset.annotate(rounded=Round("intnum", HUGE)).values_list("rounded", flat=True)


@pytest.mark.asyncio
async def test_a_bound_int_too_long_to_write_is_refused(db):
    await testmodels.IntFields.objects.create(intnum=1)
    dialect_name = testmodels.IntFields._meta.connection.dialect.name
    # SQLite refuses any int past 64 bits itself.
    error_type = OperationalError if dialect_name == "sqlite" else ValidationError
    for annotation in (Value(HUGE), Left(Value("abc"), HUGE), F("intnum") + HUGE):
        with pytest.raises(error_type):
            await testmodels.IntFields.objects.annotate(result=annotation).values_list("result", flat=True)
