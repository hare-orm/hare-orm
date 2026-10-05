import asyncio
from uuid import uuid4

import pytest

from hare.exceptions import ConfigurationError, DoesNotExist, IncompleteInstanceError, IntegrityError, QueryError
from tests.testmodels import (
    Author,
    VersionedDirtyTrackedDocument,
    VersionedDocument,
    VersionedDocumentWithAuthor,
    VersionedDocumentWithComputedColumns,
    VersionedDocumentWithExcludedField,
    VersionedDocumentWithJson,
    VersionedDocumentWithReviewer,
    VersionedDocumentWithSoftDelete,
)


@pytest.mark.asyncio
async def test_create_starts_at_version_1(db):
    doc = await VersionedDocument.objects.create(title="Draft")
    assert doc.version == 1
    assert doc.pk == (doc.id, 1)


@pytest.mark.asyncio
async def test_create_new_version_shares_id_increments_version(db):
    v1 = await VersionedDocument.objects.create(title="Draft")
    v2 = await v1.create_new_version(title="Revised")

    assert v2.id == v1.id
    assert v2.version == v1.version + 1 == 2
    assert v2.title == "Revised"
    # the old row is untouched, not mutated - both physically exist
    assert (await VersionedDocument.objects.get(id=v1.id, version=1)).title == "Draft"
    assert (await VersionedDocument.objects.get(id=v1.id, version=2)).title == "Revised"


@pytest.mark.asyncio
async def test_get_new_version_does_not_save(db):
    v1 = await VersionedDocument.objects.create(title="Draft")
    v2 = v1.get_new_version(title="Revised")

    assert v2.version == 2
    assert not await VersionedDocument.objects.filter(id=v1.id, version=2).exists()


@pytest.mark.asyncio
async def test_get_new_version_rejects_id_and_version_overrides(db):
    """get_new_version()'s **kwargs were applied via a plain dict.update() AFTER id/version were
    set - a caller accidentally passing id= or version= (e.g. forwarding a dict that happens to
    include them) silently broke the "same id, version+1" invariant this whole mixin exists to
    guarantee, with no error at all."""
    v1 = await VersionedDocument.objects.create(title="Draft")

    with pytest.raises(QueryError):
        v1.get_new_version(id=uuid4(), title="Revised")

    with pytest.raises(QueryError):
        v1.get_new_version(version=99, title="Revised")


@pytest.mark.asyncio
async def test_get_new_version_does_not_carry_forward_soft_delete_state(db):
    """A plain create()/Model(**kwargs) call leaves an omitted field at its class default (None -
    not deleted) - get_new_version() cloning self's own deleted_at value instead used to carry a
    deleted instance's timestamp onto its brand-new "next version", making it born already
    invisible to every soft-delete-scoped query and defeating the point of versioning it at all."""
    v1 = await VersionedDocumentWithSoftDelete.objects.create(title="Draft")
    await v1.delete()
    assert v1.deleted_at is not None

    v2 = await v1.create_new_version(title="Revised")

    assert v2.deleted_at is None
    assert await VersionedDocumentWithSoftDelete.objects.filter(id=v1.id, version=v2.version).exists()


@pytest.mark.asyncio
async def test_get_last_version_or_exception_sees_a_soft_deleted_latest_version(db):
    """The default manager's automatic "WHERE deleted_at IS NULL" used to make
    get_last_version_or_exception() silently return an OLDER, non-deleted version instead of the
    true highest one whenever the actual latest version was soft-deleted - defeating its own
    "highest version" contract with no error at all. Worse, feeding that stale version into
    create_new_version() then computed version+1 from it, colliding with the already-existing
    (soft-deleted) row at that same version number and raising IntegrityError."""
    v1 = await VersionedDocumentWithSoftDelete.objects.create(title="v1")
    v2 = await v1.create_new_version(title="v2")
    await v2.delete()

    latest = await VersionedDocumentWithSoftDelete.get_last_version_or_exception(id=v1.id)
    assert latest.version == 2
    assert latest.deleted_at is not None

    v3 = await latest.create_new_version(title="v3")
    assert v3.version == 3


@pytest.mark.asyncio
async def test_get_new_version_does_not_alias_mutable_field_values(db):
    """A mutable field value (JSONField) on the new version must be its own independent copy -
    mutating it in place must not also corrupt the instance get_new_version() was called on."""
    v1 = await VersionedDocumentWithJson.objects.create(title="Draft", data={"tags": ["a"]})
    v2 = v1.get_new_version(title="Revised")

    assert v2.data == v1.data
    assert v2.data is not v1.data

    v2.data["tags"].append("b")
    assert v1.data == {"tags": ["a"]}
    assert v2.data == {"tags": ["a", "b"]}


@pytest.mark.asyncio
async def test_get_last_version_or_exception_returns_highest(db):
    v1 = await VersionedDocument.objects.create(title="Draft")
    v2 = await v1.create_new_version(title="Revised")
    await v2.create_new_version(title="Final")

    latest = await VersionedDocument.get_last_version_or_exception(id=v1.id)
    assert latest.version == 3
    assert latest.title == "Final"


@pytest.mark.asyncio
async def test_get_last_version_or_exception_raises_when_missing(db):
    """A syntactically-valid but nonexistent UUID - "does-not-exist" itself isn't a valid UUID,
    which SQLite's weakly-typed columns accept but Postgres's native UUID column rejects at the
    driver level before the query even runs, masking the actual not-found behavior being tested."""
    with pytest.raises(DoesNotExist):
        await VersionedDocument.get_last_version_or_exception(id=uuid4())


@pytest.mark.asyncio
async def test_get_last_version_or_exception_custom_exception(db):
    class CustomNotFound(Exception):
        pass

    with pytest.raises(CustomNotFound):
        await VersionedDocument.get_last_version_or_exception(id=uuid4(), exception=CustomNotFound)


@pytest.mark.asyncio
async def test_new_version_excluded_fields_resets_to_default(db):
    v1 = await VersionedDocumentWithExcludedField.objects.create(title="Draft", single_use_token="secret-1")
    v2 = await v1.create_new_version(title="Revised")

    assert v2.single_use_token == "unused"  # reset to the field's own default, not copied


@pytest.mark.asyncio
async def test_multiple_documents_do_not_collide(db):
    doc_a = await VersionedDocument.objects.create(title="A")
    doc_b = await VersionedDocument.objects.create(title="B")
    await doc_a.create_new_version(title="A2")

    assert await VersionedDocument.objects.filter(id=doc_a.id).count() == 2
    assert await VersionedDocument.objects.filter(id=doc_b.id).count() == 1


@pytest.mark.asyncio
async def test_duplicate_id_version_pair_raises_integrity_error(db):
    """The composite PK is the actual uniqueness guarantee this whole feature relies on for
    safe concurrent create_new_version() races (see the DB-level constraint it depends on)."""
    v1 = await VersionedDocument.objects.create(title="Draft")
    with pytest.raises(IntegrityError):
        await VersionedDocument.objects.create(id=v1.id, version=v1.version, title="Collision")


@pytest.mark.asyncio
async def test_concurrent_create_new_version_one_wins_one_raises(db):
    """Mirrors the real workaround this mixin replaces - service-core's ScriptService relies on
    exactly this DB-level uniqueness to resolve a race between two concurrent edit attempts."""
    v1 = await VersionedDocument.objects.create(title="Draft")

    results = await asyncio.gather(
        v1.create_new_version(title="From task A"),
        v1.create_new_version(title="From task B"),
        return_exceptions=True,
    )
    successes = [r for r in results if not isinstance(r, BaseException)]
    failures = [r for r in results if isinstance(r, BaseException)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], IntegrityError)


# ============================================================================
# Further combinations: select_related/prefetch_related on a regular forward FK, .only()/
# .defer(), bulk_create()/bulk_update(), track_dirty_fields, Transactions.autonomous(),
# get_new_version() on a partial instance, optimistic_lock_field colliding with the pk
# ============================================================================


@pytest.mark.asyncio
async def test_select_related_on_regular_forward_fk(db):
    """The FK is just an ordinary field, unrelated to id/version - select_related() works on it
    exactly like it would on any non-VersionedModel model."""
    author = await Author.objects.create(name="A. Uthor")
    await VersionedDocumentWithAuthor.objects.create(title="Draft", author=author)

    fetched = await VersionedDocumentWithAuthor.objects.filter(title="Draft").select_related("author").first()
    assert fetched.author.name == "A. Uthor"


@pytest.mark.asyncio
async def test_prefetch_related_on_regular_forward_fk(db):
    author = await Author.objects.create(name="A. Uthor")
    await VersionedDocumentWithAuthor.objects.create(title="Draft", author=author)

    fetched = await VersionedDocumentWithAuthor.objects.filter(title="Draft").prefetch_related("author").first()
    assert fetched.author.name == "A. Uthor"


@pytest.mark.asyncio
async def test_only_excludes_non_pk_fields(db):
    doc = await VersionedDocument.objects.create(title="Draft")

    partial = await VersionedDocument.objects.get(pk=doc.pk).only("id", "version")
    assert "title" not in partial.__dict__


@pytest.mark.asyncio
async def test_defer_can_exclude_a_regular_field(db):
    doc = await VersionedDocument.objects.create(title="Draft")

    partial = await VersionedDocument.objects.get(pk=doc.pk).defer("title")
    assert "title" not in partial.__dict__
    # id/version - the pk - stay loaded regardless, same as .defer() on any model.
    assert partial.id == doc.id
    assert partial.version == doc.version


@pytest.mark.asyncio
async def test_bulk_create(db):
    await VersionedDocument.objects.bulk_create(
        [VersionedDocument(title="A"), VersionedDocument(title="B")],
    )
    assert await VersionedDocument.objects.all().count() == 2


@pytest.mark.asyncio
async def test_bulk_update(db):
    doc = await VersionedDocument.objects.create(title="Draft")
    doc.title = "Updated"

    await VersionedDocument.objects.bulk_update([doc], fields=["title"])

    assert (await VersionedDocument.objects.get(pk=doc.pk)).title == "Updated"


@pytest.mark.asyncio
async def test_track_dirty_fields(db):
    doc = await VersionedDirtyTrackedDocument.objects.create(title="Draft")
    assert doc.get_dirty_fields() == {}

    doc.title = "Updated"
    assert doc.get_dirty_fields() == {"title": ("Draft", "Updated")}

    await doc.save()
    assert doc.get_dirty_fields() == {}


@pytest.mark.asyncio
async def test_autonomous_transaction(db, tmp_path):
    """A real file-backed SQLite DB, not the shared in-memory `db` fixture connection -
    Transactions.autonomous() opens a genuinely separate connection."""
    from hare.contrib.test.isolated_contexts import hare_test_context
    from hare.transactions.transactions import Transactions

    db_path = tmp_path / "versioned_autonomous_test.sqlite"
    async with hare_test_context(
        ["tests.testmodels"], db_url=f"sqlite+aiosqlite:///{db_path}?synchronous=OFF", connection_label="models"
    ):
        async with Transactions.autonomous() as conn:
            await VersionedDocument.objects.using(conn).create(title="Draft")

        assert await VersionedDocument.objects.filter(title="Draft").exists()


@pytest.mark.asyncio
async def test_get_new_version_on_partial_instance_raises(db):
    """A .only()-truncated instance can't be cloned field-for-field - a dedicated error names the
    missing fields instead of a bare AttributeError; passing them explicitly makes it work."""
    doc = await VersionedDocument.objects.create(title="Draft")
    partial = await VersionedDocument.objects.get(pk=doc.pk).only("id", "version")

    with pytest.raises(IncompleteInstanceError, match="title"):
        partial.get_new_version()

    new_version = await partial.create_new_version(title="Explicit")
    assert (await VersionedDocument.objects.get(id=doc.id, version=2)).title == "Explicit"
    assert new_version.version == 2


@pytest.mark.asyncio
async def test_new_version_of_partial_instance_skips_computed_and_auto_now_columns(db):
    document = await VersionedDocumentWithComputedColumns.objects.create(title="Draft", price=3, quantity=4)
    without_generated = await VersionedDocumentWithComputedColumns.objects.get(pk=document.pk).defer("total")
    second_version = await without_generated.create_new_version()
    assert (second_version.version, second_version.total) == (2, 12)

    without_computed = await VersionedDocumentWithComputedColumns.objects.get(id=document.id, version=2).only(
        "id", "version", "title", "price", "quantity", "created"
    )
    third_version = await without_computed.create_new_version(price=5)
    fetched = await VersionedDocumentWithComputedColumns.objects.get(id=document.id, version=3)
    assert (fetched.total, fetched.created) == (20, document.created)
    assert fetched.modified is not None
    assert third_version.version == 3

    with pytest.raises(IncompleteInstanceError, match="created"):
        (await VersionedDocumentWithComputedColumns.objects.get(pk=document.pk).defer("created")).get_new_version()


@pytest.mark.asyncio
async def test_create_new_version_overrides_fk_by_relation_name(db):
    first_author = await Author.objects.create(name="First")
    second_author = await Author.objects.create(name="Second")
    doc = await VersionedDocumentWithReviewer.objects.create(title="Draft", author=first_author)

    new_version = await doc.create_new_version(author=second_author)

    assert new_version.author_id == second_author.id
    fetched = await VersionedDocumentWithReviewer.objects.get(id=doc.id, version=2)
    assert fetched.author_id == second_author.id


@pytest.mark.asyncio
async def test_create_new_version_overrides_fk_by_shadow_column(db):
    first_author = await Author.objects.create(name="First")
    second_author = await Author.objects.create(name="Second")
    doc = await VersionedDocumentWithReviewer.objects.create(title="Draft", author=first_author)

    await doc.create_new_version(author_id=second_author.id)

    assert (await VersionedDocumentWithReviewer.objects.get(id=doc.id, version=2)).author_id == second_author.id


@pytest.mark.asyncio
async def test_new_version_excluded_fields_resets_fk_listed_by_relation_name(db):
    author = await Author.objects.create(name="Author")
    reviewer = await Author.objects.create(name="Reviewer")
    doc = await VersionedDocumentWithReviewer.objects.create(title="Draft", author=author, reviewer=reviewer)

    await doc.create_new_version(title="Revised")

    fetched = await VersionedDocumentWithReviewer.objects.get(id=doc.id, version=2)
    assert fetched.reviewer_id is None
    assert fetched.author_id == author.id


@pytest.mark.asyncio
async def test_optimistic_lock_field_conflicting_with_pk_rejected():
    """Meta.optimistic_lock_field can't also name a primary-key column - optimistic_lock_field means "bump this
    column in place on save()", which conflicts with a pk column identifying which row is being
    saved (confirmed to corrupt the UPDATE's bound parameter count at execution time otherwise)."""
    from hare.contrib.test.isolated_contexts import hare_test_context

    with pytest.raises(ConfigurationError, match="primary key"):
        async with hare_test_context(["tests.model_setup.model_versioned_pk_conflict"]):
            pass


@pytest.mark.asyncio
async def test_optimistic_lock_field_on_a_generated_field_rejected():
    """A DB-generated optimistic_lock_field was accepted, although every write bumps it in place."""
    from hare.contrib.test.isolated_contexts import hare_test_context

    with pytest.raises(ConfigurationError, match="DB-generated"):
        async with hare_test_context(["tests.model_setup.model_optimistic_lock_field_generated"]):
            pass
