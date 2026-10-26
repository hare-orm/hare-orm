"""A delete's preview on ClickHouse, which keeps no foreign keys - hare checks RESTRICT itself."""

import pytest

from tests.dialects.clickhouse.models import DiamondLeft, DiamondRestrictLeaf, DiamondRight, DiamondRoot


@pytest.mark.asyncio
async def test_preview_of_a_restrict_row_hare_enforces_matches_the_delete(clickhouse_db):
    """Hare lets the delete remove a RESTRICT row the same cascade removes - the preview still listed it
    as blocking."""
    root = await DiamondRoot.objects.create(id=1)
    left = await DiamondLeft.objects.create(id=1, root=root)
    right = await DiamondRight.objects.create(id=1, root=root)
    await DiamondRestrictLeaf.objects.create(id=1, left=left, right=right)

    preview = await root.delete_preview()
    assert preview.can_delete
    assert preview.restricted_by == []
    await root.delete()
    assert await DiamondRestrictLeaf.objects.count() == 0
