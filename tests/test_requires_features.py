"""requires_features() gates a test on the connection's capabilities - its Features, its Dialect's
attributes, or the dialect's name."""

import pytest

from hare.contrib.test import requires_features


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_a_feature_of_the_connection(db):
    assert db.db().features.supports_transactions


@requires_features(supports_distinct_on=True)
@pytest.mark.asyncio
async def test_a_dialect_attribute_runs_where_the_dialect_has_it(db):
    assert db.db().dialect.supports_distinct_on


@requires_features(supports_distinct_on=False)
@pytest.mark.asyncio
async def test_a_dialect_attribute_skips_where_it_differs(db):
    assert not db.db().dialect.supports_distinct_on


@pytest.mark.asyncio
async def test_an_unknown_capability_is_an_error(db):
    @requires_features(supports_teleportation=True)
    async def gated() -> None:
        pass

    with pytest.raises(AttributeError, match="supports_teleportation"):
        await gated()
