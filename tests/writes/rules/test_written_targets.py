"""The querysets a write takes its rows from: every write refuses the same shapes, and showing a write's SQL
changes nothing of the objects it would write."""

import pytest

from hare.exceptions import QueryError
from hare.query.expressions import F
from tests.writes.rules.write_rules_models import RuleDocument


@pytest.mark.asyncio
async def test_a_bulk_update_refuses_a_sample(rules_db):
    document = await RuleDocument.objects.create(id=1, title="a")
    document.title = "b"
    with pytest.raises(QueryError, match="sample"):
        await RuleDocument.objects.sample(50).bulk_update([document], ["title"])


@pytest.mark.asyncio
async def test_showing_a_bulk_create_changes_no_object(rules_db):
    document = RuleDocument(id=1, title="a", email="a@example.com")
    RuleDocument.objects.bulk_create([document]).sql()
    assert document.created_at is None
    assert document.updated_at is None
    assert document.email_blind_index is None


@pytest.mark.asyncio
async def test_a_bulk_create_refuses_an_expression_in_a_text_field(rules_db):
    with pytest.raises(QueryError, match="title"):
        await RuleDocument.objects.bulk_create([RuleDocument(id=1, title=F("secret"))])
    assert not await RuleDocument.objects.exists()
