"""Regression tests for BulkUpdateQuery/BulkCreateQuery.sql(params_inline=True).

Both queries used to silently ignore the params_inline flag and always return SQL with bind
placeholders, unlike AwaitableQuery.sql()'s documented contract (matching QuerySet.sql()).
"""

import pytest

from hare.contrib.test import requires_features
from tests.testmodels import Tournament, UniqueName


@pytest.mark.asyncio
async def test_bulk_update_sql_params_inline_embeds_literal_values(db):
    objs = [await Tournament.objects.create(name="1"), await Tournament.objects.create(name="2")]
    objs[0].name = "renamed-one"
    objs[1].name = "renamed-two"

    placeholder_sql = Tournament.objects.bulk_update(objs, fields=["name"]).sql()
    inline_sql = Tournament.objects.bulk_update(objs, fields=["name"]).sql(params_inline=True)

    assert "renamed-one" not in placeholder_sql
    assert "renamed-two" not in placeholder_sql
    assert "renamed-one" in inline_sql
    assert "renamed-two" in inline_sql


@pytest.mark.asyncio
async def test_bulk_update_sql_params_inline_embeds_where_clause_values(db):
    """extra_where (Q-object filters alongside bulk_update) must also be inlined, not just
    the VALUES-table rows - the fix threads params_inline through the whole SQL-building
    pass, not just part of it."""
    objs = [await Tournament.objects.create(name="1", desc="keep-me")]
    objs[0].name = "updated"

    inline_sql = Tournament.objects.filter(desc="keep-me").bulk_update(objs, fields=["name"]).sql(params_inline=True)
    assert "keep-me" in inline_sql


@pytest.mark.asyncio
async def test_bulk_create_sql_params_inline_embeds_literal_values(db_truncate):
    objects = [UniqueName(name="alice"), UniqueName(name="bob")]

    placeholder_sql = UniqueName.objects.bulk_create(objects).sql()
    inline_sql = UniqueName.objects.bulk_create(objects).sql(params_inline=True)

    assert "alice" not in placeholder_sql
    assert "bob" not in placeholder_sql
    assert "alice" in inline_sql
    assert "bob" in inline_sql


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_bulk_create_sql_params_inline_with_update_fields(db_truncate):
    await UniqueName.objects.bulk_create([UniqueName(name="dup")])

    inline_sql = UniqueName.objects.bulk_create(
        [UniqueName(name="dup", optional="new-optional-value")],
        update_fields=["optional"],
        on_conflict=["name"],
    ).sql(params_inline=True)

    assert "new-optional-value" in inline_sql
    assert "ON CONFLICT" in inline_sql.upper() or "ON DUPLICATE" in inline_sql.upper()


@pytest.mark.asyncio
async def test_bulk_create_sql_params_inline_with_custom_pk(db_truncate):
    """Objects with a caller-supplied PK go through a separate generated-columns branch -
    make sure it is also covered by the inline path (not just the auto-generated-pk branch)."""
    objects = [UniqueName(id=1, name="custom-pk-name")]

    inline_sql = UniqueName.objects.bulk_create(objects).sql(params_inline=True)

    assert "custom-pk-name" in inline_sql
