import dataclasses

import pytest

from hare import Connections
from hare.contrib.test import requires_features
from hare.exceptions import ConfigurationError


@pytest.mark.asyncio
async def test_features_are_immutable(db):
    features = Connections.get("models").features
    with pytest.raises(dataclasses.FrozenInstanceError):
        features.supports_transactions = False  # type: ignore[misc]
    with pytest.raises((AttributeError, TypeError)):
        features.bar = "foo"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_replace_changes_only_the_given_features(db):
    features = Connections.get("models").features
    replaced = features.replace(max_bind_parameters=10)
    assert replaced.max_bind_parameters == 10
    assert replaced.supports_transactions == features.supports_transactions
    assert features.max_bind_parameters != 10


@pytest.mark.xfail(raises=ConfigurationError, reason="Connection 'other' does not exist")
@requires_features(connection_name="other")
@pytest.mark.asyncio
async def test_connection_name(db):
    """Will fail with a ConfigurationError since connection 'other' does not exist."""


@requires_features(dialect="sqlite")
@pytest.mark.xfail(reason="Test is expected to fail - testing xfail behavior")
@pytest.mark.asyncio
async def test_actually_runs(db):
    assert False


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_dialect_sqlite(db):
    dialect = Connections.get("models").dialect
    assert (dialect.name, dialect.otel_system_name) == ("sqlite", "sqlite")


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_dialect_postgresql(db):
    dialect = Connections.get("models").dialect
    assert (dialect.name, dialect.otel_system_name) == ("postgresql", "postgresql")
