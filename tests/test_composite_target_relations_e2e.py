"""End-to-end coverage for a ForeignKeyField/ManyToManyField targeting a composite-PK
(VersionedModel) row - the target use case composite-target FK/O2O/M2M support exists for.
Exercises create, select_related, bare-equality filter, prefetch_related (both directions), a
real DB-level orphan-reference rejection, and cascade delete together on one real
VersionedModel-derived scenario, rather than each in isolation as the component-level tests
elsewhere already do."""

import uuid

import pytest

from hare.exceptions import (
    IncompleteInstanceError,
    IntegrityError,
    QueryError,
)
from tests.testmodels import DocumentRevisionNote, VersionedArticle, VersionedDocument, VersionedTag
from tests.utils.database_under_test import DatabaseUnderTest


@pytest.mark.asyncio
async def test_composite_target_fk_partially_set_raises(db):
    """Setting only the "_id" half of a composite-target FK's shadow columns (e.g. by assigning
    the id-suffixed field directly, without its version sibling) used to silently resolve
    `.document` to None on access - indistinguishable from a genuinely unset (both columns None)
    relation. Neither is a valid reference nor a valid "no relation", so this must raise instead
    of returning a wrong result a caller can't tell apart from "not related"."""
    note = DocumentRevisionNote(note="orphaned half-reference", document_id=uuid.uuid4())
    with pytest.raises(IncompleteInstanceError):
        await note.document


@pytest.mark.asyncio
async def test_composite_target_fk_with_unset_target_component_raises(db):
    """A target row marked saved but missing part of its composite key can't be referenced -
    the FK columns would silently be stored as NULL."""
    half_known_document = VersionedDocument.construct(id=uuid.uuid4(), version=None, title="x", _saved_in_db=True)
    with pytest.raises(QueryError, match="its 'version' is None"):
        DocumentRevisionNote(document=half_known_document, note="n")


@pytest.mark.asyncio
async def test_composite_target_fk_e2e_lifecycle(db):
    doc = await VersionedDocument.objects.create(title="v1")
    note = await DocumentRevisionNote.objects.create(document=doc, note="looks good")

    fetched = await DocumentRevisionNote.objects.filter(pk=note.pk).select_related("document").first()
    assert fetched.document.title == "v1"

    results = await DocumentRevisionNote.objects.filter(document=doc)
    assert [r.pk for r in results] == [note.pk]

    prefetched_notes = await DocumentRevisionNote.objects.filter(pk=note.pk).prefetch_related("document")
    assert prefetched_notes[0].document.title == "v1"

    prefetched_docs = await VersionedDocument.objects.filter(pk=doc.pk).prefetch_related("revision_notes")
    assert [n.note for n in prefetched_docs[0].revision_notes] == ["looks good"]

    await doc.delete()
    assert await DocumentRevisionNote.objects.filter(pk=note.pk).count() == 0

    # Orphan-reference rejection last - a caught IntegrityError leaves a real Postgres
    # transaction aborted, so nothing meaningful can run in the same test after it.
    if DatabaseUnderTest.get_dialect().supports_foreign_keys:
        with pytest.raises(IntegrityError):
            await DocumentRevisionNote.objects.create(document_id=doc.id, document_version=1, note="orphan")


@pytest.mark.asyncio
async def test_composite_target_m2m_e2e_lifecycle(db):
    article = await VersionedArticle.objects.create(title="article-1")
    tag_a = await VersionedTag.objects.create(name="a")
    tag_b = await VersionedTag.objects.create(name="b")

    await article.tags.add(tag_a, tag_b)
    assert sorted(t.name for t in await article.tags.filter()) == ["a", "b"]

    filtered = await VersionedArticle.objects.filter(tags=tag_a)
    assert [a.pk for a in filtered] == [article.pk]

    prefetched_articles = await VersionedArticle.objects.filter(pk=article.pk).prefetch_related("tags")
    assert sorted(t.name for t in prefetched_articles[0].tags) == ["a", "b"]

    prefetched_tags = await VersionedTag.objects.filter(pk=tag_a.pk).prefetch_related("articles")
    assert [a.title for a in prefetched_tags[0].articles] == ["article-1"]

    await article.tags.remove(tag_a)
    assert [t.name for t in await article.tags.filter()] == ["b"]

    await article.tags.clear()
    assert list(await article.tags.filter()) == []


@pytest.mark.asyncio
async def test_composite_target_fk_prefetch_over_many_parents(db):
    """prefetch_related() in both directions of a composite-target FK over more rows than one
    OR-of-ANDs filter can hold (it used to raise RecursionError)."""
    row_count = 1500
    documents = [VersionedDocument(id=uuid.uuid4(), version=1, title=f"d{index}") for index in range(row_count)]
    await VersionedDocument.objects.bulk_create(documents)
    await DocumentRevisionNote.objects.bulk_create(
        [
            DocumentRevisionNote(document_id=document.id, document_version=1, note=document.title)
            for document in documents
        ]
    )

    notes = await DocumentRevisionNote.objects.all().prefetch_related("document")
    assert len(notes) == row_count
    assert all(note.document.title == note.note for note in notes)

    prefetched_documents = await VersionedDocument.objects.all().prefetch_related("revision_notes")
    assert len(prefetched_documents) == row_count
    assert all(
        [note.note for note in document.revision_notes] == [document.title] for document in prefetched_documents
    )


@pytest.mark.asyncio
async def test_composite_m2m_prefetch_over_many_rows(db):
    """prefetch_related() of a composite-PK M2M whose through rows and target rows both exceed one
    OR-of-ANDs filter."""
    row_count = 1500
    article = await VersionedArticle.objects.create(title="article")
    tags = [VersionedTag(id=uuid.uuid4(), version=1, name=f"t{index}") for index in range(row_count)]
    await VersionedTag.objects.bulk_create(tags)
    await article.tags.add(*tags)

    prefetched_article = (await VersionedArticle.objects.filter(pk=article.pk).prefetch_related("tags"))[0]
    assert sorted(tag.name for tag in prefetched_article.tags) == sorted(tag.name for tag in tags)

    prefetched_tags = await VersionedTag.objects.all().prefetch_related("articles")
    assert len(prefetched_tags) == row_count
    assert all([linked.title for linked in tag.articles] == ["article"] for tag in prefetched_tags)
