"""The fields a write may name: every write refuses the same fields with the same message, and an update never
writes the version or the soft-delete field - it bumps the version itself."""

import pytest

from hare.contrib.test import requires_features
from hare.exceptions import QueryError
from hare.fields.database_default import DatabaseDefault
from hare.query.expressions import F
from tests.writes.rules.write_rules_models import (
    RuleDocument,
    RuleDocumentReviewer,
    RuleDocumentTag,
    RuleLabel,
    RuleTag,
)


@requires_features(supports_merge=True)
@pytest.mark.asyncio
async def test_a_merge_update_refuses_the_version(rules_db):
    await RuleDocument.objects.create(id=1, title="a")
    with pytest.raises(QueryError, match="version"):
        await RuleDocument.objects.merge([{"id": 1, "title": "b"}], on="id").when_matched(update={"version": 5})
    assert (await RuleDocument.objects.get(id=1)).version == 0


@requires_features(supports_merge=True)
@pytest.mark.asyncio
async def test_a_merge_update_bumps_the_version(rules_db):
    await RuleDocument.objects.create(id=1, title="a")
    written = await RuleDocument.objects.merge([{"id": 1, "title": "b"}], on="id").when_matched(
        update={"title": F("merge_source__title")}
    )
    assert written == 1
    document = await RuleDocument.objects.get(id=1)
    assert (document.title, document.version) == ("b", 1)
    assert document.updated_at > document.created_at


@requires_features(supports_merge=True)
@pytest.mark.asyncio
async def test_a_merge_update_refuses_the_soft_delete_field(rules_db):
    await RuleDocument.objects.create(id=1, title="a")
    with pytest.raises(QueryError, match="deleted_at"):
        await RuleDocument.objects.merge([{"id": 1}], on="id").when_matched(update={"deleted_at": None})


@requires_features(supports_merge=True)
@pytest.mark.asyncio
async def test_a_merge_insert_writes_the_version_and_the_soft_delete_field_as_create_does(rules_db):
    written = await RuleDocument.objects.merge([{"id": 1, "title": "a"}], on="id").when_not_matched(
        insert={"id": F("merge_source__id"), "title": F("merge_source__title"), "version": 3}
    )
    assert written == 1
    assert (await RuleDocument.objects.get(id=1)).version == 3


@pytest.mark.asyncio
async def test_pk_names_the_primary_key_of_a_through_row(rules_db):
    document = await RuleDocument.objects.create(id=1, title="a")
    tag = await RuleTag.objects.create(id=1, name="t")
    await document.tags.add(tag, through_defaults={"pk": 7})
    assert await RuleDocumentTag.objects.values_list("id", flat=True) == [7]


@requires_features(supports_merge=True)
@pytest.mark.asyncio
async def test_pk_names_the_primary_key_of_a_row_a_merge_inserts(rules_db):
    rows = [{"id": 5, "title": "m"}]
    await RuleDocument.objects.merge(rows, on="id").when_not_matched(
        insert={"pk": F("merge_source__id"), "title": F("merge_source__title")}
    )
    assert await RuleDocument.objects.values_list("id", "title") == [(5, "m")]


@pytest.mark.parametrize("write", ["save", "delete"])
@pytest.mark.asyncio
async def test_an_update_of_an_object_whose_version_was_never_read_is_refused(rules_db, write):
    (document,) = await RuleDocument.objects.bulk_create([RuleDocument(id=1, title="a")])
    if not isinstance(document.version, DatabaseDefault):
        pytest.skip("the insert read the version back")
    with pytest.raises(QueryError, match="its version 'version' holds the DatabaseDefault sentinel"):
        await getattr(document, write)()
    assert isinstance(document.version, DatabaseDefault)
    assert await RuleDocument.objects.values_list("version", "deleted_at") == [(0, None)]


@pytest.mark.asyncio
async def test_a_relation_of_a_through_row_is_written_from_its_object(rules_db):
    """An object given to a relation of the through model in through_defaults was converted as the relation's
    key and failed - it is written as its key columns, as by create()."""
    document = await RuleDocument.objects.create(id=1, title="a")
    tag = await RuleTag.objects.create(id=1, name="t")
    source = await RuleLabel.objects.create(id=2, name="s")
    await document.tags.add(tag, through_defaults={"id": 1, "source": source})
    assert await RuleDocumentTag.objects.values_list("source_id", flat=True) == [2]


@pytest.mark.parametrize("given_name", ["assigned_by", "assigned_by_id"])
@pytest.mark.asyncio
async def test_a_required_relation_of_a_through_row_is_given_by_its_name(rules_db, given_name):
    """A required relation given in through_defaults by its name was refused as not given - the check looked for
    its key field's name."""
    document = await RuleDocument.objects.create(id=1, title="a")
    reviewer = await RuleLabel.objects.create(id=2, name="r")
    tag = await RuleTag.objects.create(id=3, name="t")
    await document.reviewers.add(reviewer, through_defaults={given_name: tag if given_name == "assigned_by" else 3})
    assert await RuleDocumentReviewer.objects.values_list("reviewer_id", "assigned_by_id") == [(2, 3)]
