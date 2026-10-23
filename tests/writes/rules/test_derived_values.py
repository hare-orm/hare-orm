"""The blind index of an encrypted field is written with the field by every write - computed from a plain
value, taken from another blind index for ``F()`` of an encrypted field, refused for any other expression."""

import pytest

from hare.contrib.test import requires_features
from hare.exceptions import FieldError
from hare.query.expressions import F
from tests.writes.rules.write_rules_models import RuleDocument, RuleDocumentTag, RuleTag


@requires_features(supports_merge=True)
@pytest.mark.asyncio
async def test_a_merge_writes_the_blind_index(rules_db):
    await RuleDocument.objects.create(id=1, email="old@example.com")
    written = await (
        RuleDocument.objects.merge([{"id": 1}, {"id": 2}], on="id")
        .when_matched(update={"email": "new@example.com"})
        .when_not_matched(insert={"id": F("merge_source__id"), "email": "added@example.com"})
    )
    assert written == 2
    assert await RuleDocument.objects.filter(email="new@example.com").values_list("id", flat=True) == [1]
    assert await RuleDocument.objects.filter(email="added@example.com").values_list("id", flat=True) == [2]
    assert not await RuleDocument.objects.filter(email="old@example.com").exists()


@requires_features(supports_merge=True)
@pytest.mark.asyncio
async def test_a_merge_refuses_an_expression_for_an_encrypted_field(rules_db):
    await RuleDocument.objects.create(id=1, email="old@example.com")
    with pytest.raises(FieldError, match="email"):
        await RuleDocument.objects.merge([{"id": 1, "title": "x"}], on="id").when_matched(
            update={"email": F("merge_source__title")}
        )


@pytest.mark.asyncio
async def test_an_insert_from_refuses_an_encrypted_field(rules_db):
    with pytest.raises(FieldError, match="email"):
        await RuleDocument.objects.insert_from(RuleDocument.objects.values_list("id", "email"), fields=["id", "email"])


@pytest.mark.asyncio
async def test_through_defaults_write_the_blind_index(rules_db):
    document = await RuleDocument.objects.create(id=1)
    tag = await RuleTag.objects.create(id=1, name="t")
    await document.tags.add(tag, through_defaults={"label": "first"})
    assert await RuleDocumentTag.objects.filter(label="first").count() == 1
