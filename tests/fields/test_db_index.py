from __future__ import annotations

import pytest

from hare.contrib.test import requires_features
from hare.ddl.indexes import Index
from hare.exceptions import ConfigurationError, IntegrityError
from hare.sql.terms import Field
from tests.testmodels import ModelWithIndexes, ModelWithNullSafeUniqueIndex


class CustomIndex(Index):
    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self._foo = ""


# ============================================================================
# Tests for Index hash, equality, and repr (no database needed)
# ============================================================================


def test_index_eq():
    assert Index(fields=("id",)) == Index(fields=("id",))
    assert CustomIndex(fields=("id",)) == CustomIndex(fields=("id",))
    assert Index(fields=("id", "name")) == Index(fields=["id", "name"])

    assert Index(fields=("id", "name")) != Index(fields=("name", "id"))
    assert Index(fields=("id",)) != Index(fields=("name",))
    assert CustomIndex(fields=("id",)) != Index(fields=("id",))


def test_index_hash():
    assert hash(Index(fields=("id",))) == hash(Index(fields=("id",)))
    assert hash(Index(fields=("id", "name"))) == hash(Index(fields=["id", "name"]))
    assert hash(CustomIndex(fields=("id", "name"))) == hash(CustomIndex(fields=["id", "name"]))

    assert hash(Index(fields=("id", "name"))) != hash(Index(fields=["name", "id"]))
    assert hash(Index(fields=("id",))) != hash(Index(fields=("name",)))

    indexes = {Index(fields=("id",))}
    indexes.add(Index(fields=("id",)))
    assert len(indexes) == 1
    indexes.add(CustomIndex(fields=("id",)))
    assert len(indexes) == 2
    indexes.add(Index(fields=("name",)))
    assert len(indexes) == 3


def test_index_repr():
    assert repr(Index(fields=("id",))) == "Index(fields=['id'])"
    assert repr(Index(fields=("id", "name"))) == "Index(fields=['id', 'name'])"
    assert repr(Index(fields=("id",), name="MyIndex")) == "Index(fields=['id'], name='MyIndex')"
    assert repr(Index(Field("id"))) == f"Index({str(Field('id'))})"
    assert repr(Index(Field("a"), name="Id")) == f"Index({str(Field('a'))}, name='Id')"
    with pytest.raises(ConfigurationError):
        Index(Field("id"), fields=("name",))


# ============================================================================
# Tests for ModelWithIndexes metadata (requires database fixture)
# ============================================================================


@pytest.mark.asyncio
async def test_model_with_indexes_meta(db):
    assert ModelWithIndexes._meta.indexes == [
        Index(fields=("f1", "f2")),
        Index(fields=("f3",), name="model_with_indexes__f3"),
    ]
    assert ModelWithIndexes._meta.fields_map["id"].index
    assert ModelWithIndexes._meta.fields_map["indexed"].index
    assert ModelWithIndexes._meta.fields_map["unique_indexed"].unique


@pytest.mark.asyncio
async def test_unique_index_with_coalesce_enforces_null_safe_uniqueness(db):
    """Index(unique=True) wrapping a nullable column in Coalesce() makes NULL participate in
    the uniqueness check - unlike a plain UniqueConstraint/unique=True field, where two rows
    with the same other columns and both NULL in the unique column are always allowed (SQL's
    NULL != NULL). Real use case: a NULL-safe composite unique index for an optional variant
    column, without resorting to a manual RunSQL migration. Only the ALLOWED combinations are
    exercised here - see the two dedicated rejection tests below for the duplicate cases, kept
    separate since each is the last statement in its own test (a caught IntegrityError leaves a
    real Postgres transaction aborted, so nothing meaningful can run in the same test after it -
    same convention as tests/test_composite_target_relations_e2e.py's own "orphan-reference
    rejection last" case)."""
    await ModelWithNullSafeUniqueIndex.objects.create(group="a", variant=None)

    # A different group is unaffected by the first row's NULL variant.
    await ModelWithNullSafeUniqueIndex.objects.create(group="b", variant=None)

    # A non-NULL variant still keeps ordinary per-value uniqueness within the same group.
    await ModelWithNullSafeUniqueIndex.objects.create(group="a", variant="x")
    await ModelWithNullSafeUniqueIndex.objects.create(group="a", variant="y")
    assert await ModelWithNullSafeUniqueIndex.objects.count() == 4
    assert await ModelWithNullSafeUniqueIndex.objects.count() == 4
    assert await ModelWithNullSafeUniqueIndex.objects.count() == 4


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_unique_index_with_coalesce_rejects_duplicate_null_variant(db):
    await ModelWithNullSafeUniqueIndex.objects.create(group="a", variant=None)
    with pytest.raises(IntegrityError):
        await ModelWithNullSafeUniqueIndex.objects.create(group="a", variant=None)


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_unique_index_with_coalesce_rejects_duplicate_value(db):
    await ModelWithNullSafeUniqueIndex.objects.create(group="a", variant="x")
    with pytest.raises(IntegrityError):
        await ModelWithNullSafeUniqueIndex.objects.create(group="a", variant="x")
