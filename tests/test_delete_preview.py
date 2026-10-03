from typing import Any

import pytest

from hare.contrib.test import requires_features
from hare.core.connections import Connections
from hare.dialects.enums import DialectName
from hare.exceptions import (
    IntegrityError,
    ProtectedError,
    QueryError,
)
from hare.models import DeletePreview, Model
from hare.models.tenancy import Tenancy
from hare.transactions.transactions import Transactions
from tests.testmodels import (
    DeletePreviewAttachment,
    DeletePreviewAuthor,
    DeletePreviewBinHardItem,
    DeletePreviewBinSoftGuard,
    DeletePreviewBook,
    DeletePreviewChapter,
    DeletePreviewChapterLock,
    DeletePreviewCrateBoxItem,
    DeletePreviewCrateSoftBox,
    DeletePreviewCrateSoftGuard,
    DeletePreviewDiamondLeft,
    DeletePreviewDiamondNoActionLeaf,
    DeletePreviewDiamondOwner,
    DeletePreviewDiamondRestrictLeaf,
    DeletePreviewDiamondRight,
    DeletePreviewDiamondRoot,
    DeletePreviewEditor,
    DeletePreviewHardCrate,
    DeletePreviewLoan,
    DeletePreviewPage,
    DeletePreviewReview,
    DeletePreviewShelf,
    DeletePreviewSoftBin,
    DeletePreviewSoftComment,
    DeletePreviewSoftFolder,
    DeletePreviewSoftNote,
    DeletePreviewTag,
    DeletePreviewTenantAuditedTask,
    DeletePreviewTenantHardProject,
    DeletePreviewTenantHardTask,
    DeletePreviewTenantLooseSoftTask,
    DeletePreviewTenantProject,
    DeletePreviewTenantSoftProject,
    DeletePreviewTenantTask,
    NullableCodeCity,
    NullableCodeCountry,
    NullableCodeShop,
    TransitiveProtectSelfReferential,
)

FIXTURE_MODELS: tuple[type[Model], ...] = (
    DeletePreviewAuthor,
    DeletePreviewTag,
    DeletePreviewShelf,
    DeletePreviewBook,
    DeletePreviewChapter,
    DeletePreviewPage,
    DeletePreviewChapterLock,
    DeletePreviewReview,
    DeletePreviewLoan,
    DeletePreviewEditor,
    DeletePreviewSoftFolder,
    DeletePreviewSoftNote,
    DeletePreviewAttachment,
    DeletePreviewSoftComment,
    DeletePreviewTenantProject,
    DeletePreviewTenantTask,
)
THROUGH_TABLES = ("delete_preview_author_tag", "delete_preview_book_shelf")


async def select_all_rows(model: type[Model]) -> list[dict[str, Any]]:
    """Every row of ``model``, soft-deleted and other tenants' ones included, ordered by pk."""
    query = model.objects.all_tenants() if model._meta.tenant_field else model.objects.all()
    if model._meta.soft_delete_field:
        query = query.include_deleted()
    return await query.order_by("pk").values()


async def select_through_rows(table_name: str) -> list[tuple[Any, ...]]:
    __, rows = await Connections.get("models").execute(f"SELECT * FROM {table_name}")
    return sorted((tuple(sorted(dict(row).items())) for row in rows), key=repr)


async def take_database_snapshot() -> dict[str, Any]:
    snapshot: dict[str, Any] = {model.__name__: await select_all_rows(model) for model in FIXTURE_MODELS}
    for table_name in THROUGH_TABLES:
        snapshot[table_name] = await select_through_rows(table_name)
    return snapshot


async def create_library() -> dict[str, Any]:
    """The hard-delete fixture: ``author`` with two books, three chapters, three pages (reached
    through a db_constraint=False hop), SET_NULL reviews, a SET_DEFAULT editor and M2M links -
    next to an unrelated ``other_author`` whose rows must stay untouched."""
    fallback_author = await DeletePreviewAuthor.objects.create(id=999, name="fallback")
    author = await DeletePreviewAuthor.objects.create(name="author")
    other_author = await DeletePreviewAuthor.objects.create(name="other")
    first_book = await DeletePreviewBook.objects.create(name="first", author=author)
    second_book = await DeletePreviewBook.objects.create(name="second", author=author)
    other_book = await DeletePreviewBook.objects.create(name="other", author=other_author)
    first_chapter = await DeletePreviewChapter.objects.create(name="1.1", book=first_book)
    second_chapter = await DeletePreviewChapter.objects.create(name="1.2", book=first_book)
    third_chapter = await DeletePreviewChapter.objects.create(name="2.1", book=second_book)
    await DeletePreviewChapter.objects.create(name="other", book=other_book)
    for chapter, page_name in ((first_chapter, "a"), (first_chapter, "b"), (second_chapter, "c")):
        await DeletePreviewPage.objects.create(name=page_name, chapter=chapter)
    await DeletePreviewReview.objects.create(name="r1", book=first_book)
    await DeletePreviewReview.objects.create(name="r2", book=second_book)
    await DeletePreviewReview.objects.create(name="r3", book=other_book)
    await DeletePreviewEditor.objects.create(name="e1", author=author)
    await DeletePreviewEditor.objects.create(name="e2", author=other_author)
    first_tag = await DeletePreviewTag.objects.create(name="t1")
    second_tag = await DeletePreviewTag.objects.create(name="t2")
    await author.tags.add(first_tag, second_tag)
    await other_author.tags.add(first_tag)
    first_shelf = await DeletePreviewShelf.objects.create(name="s1")
    second_shelf = await DeletePreviewShelf.objects.create(name="s2")
    await first_book.shelves.add(first_shelf, second_shelf)
    await second_book.shelves.add(first_shelf)
    await other_book.shelves.add(second_shelf)
    return {
        "fallback_author": fallback_author,
        "author": author,
        "first_book": first_book,
        "third_chapter": third_chapter,
    }


async def count_through_rows(table_name: str) -> tuple[int, int]:
    """``(total rows, rows with a NULL key column)`` of an auto-generated through table."""
    rows = await select_through_rows(table_name)
    return len(rows), sum(1 for row in rows if any(value is None for __, value in row))


async def measure_library_state() -> dict[str, Any]:
    return {
        "row_counts": {model: len(await select_all_rows(model)) for model in FIXTURE_MODELS},
        "nulled_reviews": await DeletePreviewReview.objects.filter(book_id__isnull=True).count(),
        "default_editors": await DeletePreviewEditor.objects.filter(author_id=999).count(),
        "through_rows": {table_name: await count_through_rows(table_name) for table_name in THROUGH_TABLES},
    }


@pytest.mark.asyncio
async def test_preview_counts_match_what_delete_really_does(db):
    library = await create_library()
    before = await measure_library_state()

    preview = await library["author"].delete_preview()
    await library["author"].delete()
    after = await measure_library_state()

    really_deleted = {
        model: before["row_counts"][model] - after["row_counts"][model]
        for model in FIXTURE_MODELS
        if before["row_counts"][model] != after["row_counts"][model]
    }
    assert preview.deleted == really_deleted
    assert preview.deleted == {
        DeletePreviewAuthor: 1,
        DeletePreviewBook: 2,
        DeletePreviewChapter: 3,
        DeletePreviewPage: 3,
    }
    assert preview.soft_deleted == {}
    assert (
        preview.nulled
        == {DeletePreviewReview: after["nulled_reviews"] - before["nulled_reviews"]}
        == {DeletePreviewReview: 2}
    )
    assert (
        preview.set_default
        == {DeletePreviewEditor: after["default_editors"] - before["default_editors"]}
        == {DeletePreviewEditor: 1}
    )
    author_tag_before, __ = before["through_rows"]["delete_preview_author_tag"]
    author_tag_after, __ = after["through_rows"]["delete_preview_author_tag"]
    assert (
        preview.m2m_through
        == {"delete_preview_author_tag": author_tag_before - author_tag_after}
        == {"delete_preview_author_tag": 2}
    )
    book_shelf_total_before, book_shelf_nulled_before = before["through_rows"]["delete_preview_book_shelf"]
    book_shelf_total_after, book_shelf_nulled_after = after["through_rows"]["delete_preview_book_shelf"]
    assert book_shelf_total_before == book_shelf_total_after
    assert (
        preview.m2m_through_nulled
        == {"delete_preview_book_shelf": book_shelf_nulled_after - book_shelf_nulled_before}
        == {"delete_preview_book_shelf": 3}
    )
    assert preview.protected_by == []
    assert preview.restricted_by == []
    assert preview.can_delete


@pytest.mark.asyncio
async def test_preview_writes_nothing(db):
    library = await create_library()
    folder = await create_folder()
    project = await DeletePreviewTenantProject.objects.create(name="p", company_id=1)
    await DeletePreviewTenantTask.objects.create(name="t", company_id=2, project=project)
    before = await take_database_snapshot()

    await library["author"].delete_preview()
    await library["first_book"].delete_preview()
    await folder.delete_preview()
    with Tenancy.scope(1):
        await project.delete_preview()

    assert await take_database_snapshot() == before
    assert folder.deleted_at is None


@pytest.mark.asyncio
async def test_preview_lists_a_transitive_protect_on_the_second_cascade_level(db):
    library = await create_library()
    lock = await DeletePreviewChapterLock.objects.create(name="lock", chapter=library["third_chapter"])

    preview = await library["author"].delete_preview()

    assert preview.protected_by == [lock]
    assert not preview.can_delete
    assert preview.deleted[DeletePreviewChapter] == 3
    with pytest.raises(ProtectedError) as exc_info:
        await library["author"].delete()
    assert exc_info.value.protected_objects == [lock]


@pytest.mark.asyncio
async def test_preview_lists_restricting_rows(db):
    library = await create_library()
    loan = await DeletePreviewLoan.objects.create(name="loan", book=library["first_book"])

    preview = await library["author"].delete_preview()

    assert preview.restricted_by == [loan]
    assert preview.protected_by == []
    assert not preview.can_delete
    with pytest.raises(IntegrityError):
        await library["author"].delete()


@pytest.mark.asyncio
async def test_preview_set_null_of_a_single_row(db):
    author = await DeletePreviewAuthor.objects.create(name="author")
    book = await DeletePreviewBook.objects.create(name="book", author=author)
    await DeletePreviewReview.objects.create(name="review", book=book)
    await DeletePreviewReview.objects.create(name="unrelated")

    preview = await book.delete_preview()

    assert preview.deleted == {DeletePreviewBook: 1}
    assert preview.nulled == {DeletePreviewReview: 1}
    assert preview.set_default == {}


@pytest.mark.asyncio
async def test_preview_m2m_through_rows(db):
    author = await DeletePreviewAuthor.objects.create(name="author")
    other_author = await DeletePreviewAuthor.objects.create(name="other")
    tags = [await DeletePreviewTag.objects.create(name=f"t{index}") for index in range(3)]
    await author.tags.add(*tags)
    await other_author.tags.add(tags[0])

    author_preview = await author.delete_preview()
    tag_preview = await tags[0].delete_preview()

    assert author_preview.m2m_through == {"delete_preview_author_tag": 3}
    assert author_preview.deleted == {DeletePreviewAuthor: 1}
    assert tag_preview.m2m_through == {"delete_preview_author_tag": 2}
    assert tag_preview.deleted == {DeletePreviewTag: 1}
    await tags[0].delete()
    assert await author.tags.all().count() == 2
    assert await other_author.tags.all().count() == 0


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_preview_sees_rows_of_the_surrounding_transaction(db_truncate):
    async with Transactions.atomic() as connection:
        author = await DeletePreviewAuthor.objects.using(connection).create(name="author")
        await DeletePreviewBook.objects.using(connection).create(name="book", author=author)

        preview = await author.delete_preview()
        preview_through_connection = await author.delete_preview(using=connection)

    assert preview.deleted == preview_through_connection.deleted == {DeletePreviewAuthor: 1, DeletePreviewBook: 1}
    assert await DeletePreviewBook.objects.all().count() == 1


async def create_folder() -> DeletePreviewSoftFolder:
    """The soft-delete fixture: a live and an already soft-deleted note, three hard-delete
    attachments and two SET_NULL comments below them."""
    folder = await DeletePreviewSoftFolder.objects.create(name="folder")
    live_note = await DeletePreviewSoftNote.objects.create(name="live", folder=folder)
    deleted_note = await DeletePreviewSoftNote.objects.create(name="deleted", folder=folder)
    await deleted_note.delete()
    await DeletePreviewAttachment.objects.create(name="a1", note=live_note)
    await DeletePreviewAttachment.objects.create(name="a2", note=live_note)
    await DeletePreviewAttachment.objects.create(name="a3", note=deleted_note)
    await DeletePreviewSoftComment.objects.create(name="c1", note=live_note)
    await DeletePreviewSoftComment.objects.create(name="c2", note=deleted_note)
    other_folder = await DeletePreviewSoftFolder.objects.create(name="other")
    await DeletePreviewSoftNote.objects.create(name="other", folder=other_folder)
    return folder


async def measure_folder_state() -> dict[str, int]:
    return {
        "live_folders": await DeletePreviewSoftFolder.objects.all().count(),
        "live_notes": await DeletePreviewSoftNote.objects.all().count(),
        "attachments": await DeletePreviewAttachment.objects.all().count(),
        "nulled_comments": await DeletePreviewSoftComment.objects.filter(note_id__isnull=True).count(),
    }


@pytest.mark.asyncio
async def test_preview_of_a_soft_delete_model_matches_delete(db):
    folder = await create_folder()
    before = await measure_folder_state()

    preview = await folder.delete_preview()
    await folder.delete()
    after = await measure_folder_state()

    assert (
        preview.soft_deleted
        == {
            DeletePreviewSoftFolder: before["live_folders"] - after["live_folders"],
            DeletePreviewSoftNote: before["live_notes"] - after["live_notes"],
        }
        == {DeletePreviewSoftFolder: 1, DeletePreviewSoftNote: 1}
    )
    assert (
        preview.deleted
        == {DeletePreviewAttachment: before["attachments"] - after["attachments"]}
        == {DeletePreviewAttachment: 3}
    )
    assert (
        preview.nulled
        == {DeletePreviewSoftComment: after["nulled_comments"] - before["nulled_comments"]}
        == {DeletePreviewSoftComment: 2}
    )


@pytest.mark.asyncio
async def test_preview_of_an_already_soft_deleted_instance_is_empty(db):
    folder = await create_folder()
    await folder.delete()

    assert await folder.delete_preview() == DeletePreview()


@pytest.mark.asyncio
async def test_preview_of_a_tenant_model_counts_what_delete_touches(db):
    project = await DeletePreviewTenantProject.objects.create(name="p1", company_id=1)
    await DeletePreviewTenantTask.objects.create(name="own tenant", company_id=1, project=project)
    await DeletePreviewTenantTask.objects.create(name="other tenant, same project", company_id=2, project=project)
    other_project = await DeletePreviewTenantProject.objects.create(name="p2", company_id=2)
    await DeletePreviewTenantTask.objects.create(name="other tenant", company_id=2, project=other_project)

    with Tenancy.scope(1):
        preview = await project.delete_preview()
        await project.delete()

    assert preview.deleted == {DeletePreviewTenantProject: 1, DeletePreviewTenantTask: 2}
    assert await DeletePreviewTenantProject.objects.all_tenants().count() == 1
    assert await DeletePreviewTenantTask.objects.all_tenants().values_list("name", flat=True) == ["other tenant"]


@pytest.mark.asyncio
async def test_preview_of_a_tenant_model_rejects_a_mismatching_tenant_scope(db):
    project = await DeletePreviewTenantProject.objects.create(name="p1", company_id=1)

    with Tenancy.scope(2), pytest.raises(QueryError):
        await project.delete_preview()


@pytest.mark.asyncio
async def test_preview_of_an_unsaved_instance_raises(db):
    with pytest.raises(QueryError):
        await DeletePreviewAuthor(name="unsaved").delete_preview()


@pytest.mark.asyncio
async def test_preview_ignores_a_protect_from_inside_the_cascade_tree_but_lists_one_from_outside(db):
    root = await TransitiveProtectSelfReferential.objects.create(name="root")
    child = await TransitiveProtectSelfReferential.objects.create(name="child", parent=root)
    await TransitiveProtectSelfReferential.objects.create(name="grandchild", parent=child, guardian=child)

    inside_only_preview = await root.delete_preview()
    outsider = await TransitiveProtectSelfReferential.objects.create(name="outsider", guardian=child)
    with_outsider_preview = await root.delete_preview()

    assert inside_only_preview.protected_by == []
    assert inside_only_preview.deleted == {TransitiveProtectSelfReferential: 3}
    assert with_outsider_preview.protected_by == [outsider]
    with pytest.raises(ProtectedError) as exc_info:
        await root.delete()
    assert exc_info.value.protected_objects == [outsider]


DIAMOND_MODELS: tuple[type[Model], ...] = (
    DeletePreviewDiamondOwner,
    DeletePreviewDiamondRoot,
    DeletePreviewDiamondLeft,
    DeletePreviewDiamondRight,
    DeletePreviewDiamondNoActionLeaf,
    DeletePreviewDiamondRestrictLeaf,
)


async def count_total_and_live_rows(models: tuple[type[Model], ...]) -> dict[type[Model], tuple[int, int]]:
    """``(every row, rows not soft-deleted)`` per model, across every tenant."""
    row_counts = {}
    for model in models:
        rows = await select_all_rows(model)
        soft_delete_field = model._meta.soft_delete_field
        live_count = sum(1 for row in rows if not soft_delete_field or row[soft_delete_field] is None)
        row_counts[model] = (len(rows), live_count)
    return row_counts


async def preview_and_delete(instance: Model, models: tuple[type[Model], ...]) -> DeletePreview:
    """Previews deleting ``instance``, really deletes it and checks the preview against what
    changed in ``models`` - or, for a preview that says the delete is blocked, that it fails
    and changes nothing."""
    before = await count_total_and_live_rows(models)
    preview = await instance.delete_preview()
    if not preview.can_delete:
        with pytest.raises((IntegrityError, ProtectedError)):
            async with Transactions.atomic():
                await instance.delete()
        assert await count_total_and_live_rows(models) == before
        return preview
    await instance.delete()
    after = await count_total_and_live_rows(models)
    really_deleted = {
        model: before[model][0] - after[model][0] for model in models if before[model][0] != after[model][0]
    }
    really_soft_deleted = {
        model: (before[model][1] - after[model][1]) - (before[model][0] - after[model][0])
        for model in models
        if (before[model][1] - after[model][1]) != (before[model][0] - after[model][0])
    }
    assert preview.deleted == really_deleted
    assert preview.soft_deleted == really_soft_deleted
    return preview


async def create_diamond(leaf_model: type[Model], owner: DeletePreviewDiamondOwner | None = None) -> Any:
    """A diamond root whose leaf is removed through its left branch and guarded by its right one."""
    root = await DeletePreviewDiamondRoot.objects.create(name="root", owner=owner)
    left = await DeletePreviewDiamondLeft.objects.create(root=root)
    right = await DeletePreviewDiamondRight.objects.create(root=root)
    await leaf_model.objects.create(left=left, right=right)
    return root


def restrict_is_checked_at_statement_end() -> bool:
    return Connections.get("models").dialect.name != DialectName.SQLITE


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_preview_ignores_a_no_action_row_the_same_delete_statement_removes(db):
    root = await create_diamond(DeletePreviewDiamondNoActionLeaf)

    preview = await preview_and_delete(root, DIAMOND_MODELS)

    assert preview.restricted_by == []
    assert preview.deleted == {
        DeletePreviewDiamondRoot: 1,
        DeletePreviewDiamondLeft: 1,
        DeletePreviewDiamondRight: 1,
        DeletePreviewDiamondNoActionLeaf: 1,
    }


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_preview_of_a_restrict_row_the_same_delete_statement_removes_follows_the_dialect(db):
    root = await create_diamond(DeletePreviewDiamondRestrictLeaf)

    preview = await preview_and_delete(root, DIAMOND_MODELS)

    assert preview.can_delete == restrict_is_checked_at_statement_end()


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_preview_still_lists_a_no_action_row_outside_the_cascade(db):
    root = await create_diamond(DeletePreviewDiamondNoActionLeaf)
    other_root = await DeletePreviewDiamondRoot.objects.create(name="other")
    other_left = await DeletePreviewDiamondLeft.objects.create(root=other_root)
    outsider = await DeletePreviewDiamondNoActionLeaf.objects.create(
        left=other_left, right=await DeletePreviewDiamondRight.objects.get(root=root)
    )

    preview = await preview_and_delete(root, DIAMOND_MODELS)

    assert preview.restricted_by == [outsider]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_preview_of_a_soft_owner_ignores_rows_its_hard_child_statement_removes(db):
    owner = await DeletePreviewDiamondOwner.objects.create(name="owner")
    await create_diamond(DeletePreviewDiamondNoActionLeaf, owner=owner)
    await create_diamond(DeletePreviewDiamondRestrictLeaf, owner=owner)

    preview = await preview_and_delete(owner, DIAMOND_MODELS)

    assert preview.can_delete == restrict_is_checked_at_statement_end()
    if preview.can_delete:
        assert preview.soft_deleted == {DeletePreviewDiamondOwner: 1}
        assert preview.deleted[DeletePreviewDiamondRoot] == 2


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_preview_lists_a_protect_from_a_soft_deleted_row_of_the_same_cascade(db):
    soft_bin = await DeletePreviewSoftBin.objects.create(name="bin")
    item = await DeletePreviewBinHardItem.objects.create(bin=soft_bin)
    guard = await DeletePreviewBinSoftGuard.objects.create(bin=soft_bin, item=item)

    preview = await preview_and_delete(
        soft_bin, (DeletePreviewSoftBin, DeletePreviewBinHardItem, DeletePreviewBinSoftGuard)
    )

    assert preview.protected_by == [guard]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_preview_lists_a_protect_from_a_soft_deleted_row_below_a_hard_root(db):
    crate = await DeletePreviewHardCrate.objects.create(name="crate")
    box = await DeletePreviewCrateSoftBox.objects.create(crate=crate)
    item = await DeletePreviewCrateBoxItem.objects.create(box=box)
    guard = await DeletePreviewCrateSoftGuard.objects.create(crate=crate, item=item)
    models = (
        DeletePreviewHardCrate,
        DeletePreviewCrateSoftBox,
        DeletePreviewCrateBoxItem,
        DeletePreviewCrateSoftGuard,
    )

    preview = await preview_and_delete(crate, models)

    assert preview.protected_by == [guard]


@pytest.mark.asyncio
async def test_preview_of_a_hard_root_with_a_soft_child_below_an_unconstrained_edge(db):
    crate = await DeletePreviewHardCrate.objects.create(name="crate")
    box = await DeletePreviewCrateSoftBox.objects.create(crate=crate)
    await DeletePreviewCrateBoxItem.objects.create(box=box)
    models = (
        DeletePreviewHardCrate,
        DeletePreviewCrateSoftBox,
        DeletePreviewCrateBoxItem,
        DeletePreviewCrateSoftGuard,
    )

    preview = await preview_and_delete(crate, models)

    assert preview.soft_deleted == {DeletePreviewCrateSoftBox: 1}
    assert preview.deleted == {DeletePreviewHardCrate: 1, DeletePreviewCrateBoxItem: 1}


@pytest.mark.asyncio
async def test_soft_delete_cascade_deletes_hard_children_of_another_tenant(db):
    with Tenancy.scope(1):
        project = await DeletePreviewTenantSoftProject.objects.create(company_id=1)
    # No tenant active: a cross-tenant link is written as trusted seed data.
    await DeletePreviewTenantHardTask.objects.create(company_id=2, project=project)
    audited_task = await DeletePreviewTenantAuditedTask.objects.create(company_id=2, project=project)
    DeletePreviewTenantAuditedTask.delete_override_calls.clear()

    with Tenancy.scope(1):
        preview = await preview_and_delete(
            project, (DeletePreviewTenantSoftProject, DeletePreviewTenantHardTask, DeletePreviewTenantAuditedTask)
        )

    assert preview.soft_deleted == {DeletePreviewTenantSoftProject: 1}
    assert preview.deleted == {DeletePreviewTenantHardTask: 1, DeletePreviewTenantAuditedTask: 1}
    assert DeletePreviewTenantAuditedTask.delete_override_calls == [audited_task.pk]


@pytest.mark.asyncio
async def test_hard_delete_cascade_soft_deletes_an_unconstrained_child_of_another_tenant(db):
    with Tenancy.scope(1):
        project = await DeletePreviewTenantHardProject.objects.create(company_id=1)
    # No tenant active: a cross-tenant link is written as trusted seed data.
    task = await DeletePreviewTenantLooseSoftTask.objects.create(company_id=2, project=project)

    with Tenancy.scope(1):
        preview = await preview_and_delete(project, (DeletePreviewTenantHardProject, DeletePreviewTenantLooseSoftTask))

    assert preview.deleted == {DeletePreviewTenantHardProject: 1}
    assert preview.soft_deleted == {DeletePreviewTenantLooseSoftTask: 1}
    with Tenancy.scope(2):
        assert await DeletePreviewTenantLooseSoftTask.objects.all().only_deleted().values_list("pk", flat=True) == [
            task.pk
        ]


@pytest.mark.asyncio
async def test_cascade_into_another_tenant_keeps_the_root_tenant_guard(db):
    with Tenancy.scope(1):
        project = await DeletePreviewTenantSoftProject.objects.create(company_id=1)
        await DeletePreviewTenantHardTask.objects.create(company_id=1, project=project)

    with Tenancy.scope(2), pytest.raises(QueryError):
        await project.delete()
    assert await DeletePreviewTenantSoftProject.objects.all_tenants().filter(deleted_at__isnull=True).count() == 1
    assert await DeletePreviewTenantHardTask.objects.all_tenants().count() == 1


@pytest.mark.asyncio
async def test_preview_of_a_partially_loaded_already_soft_deleted_instance_is_empty(db):
    folder = await create_folder()
    await folder.delete()

    partial_folder = await DeletePreviewSoftFolder.objects.all().only_deleted().only("id", "name").get(pk=folder.pk)

    assert await partial_folder.delete_preview() == DeletePreview()


@pytest.mark.asyncio
async def test_preview_of_a_partially_loaded_live_instance_matches_delete(db):
    folder = await create_folder()
    partial_folder = await DeletePreviewSoftFolder.objects.all().only("id", "name").get(pk=folder.pk)

    preview = await preview_and_delete(
        partial_folder, (DeletePreviewSoftFolder, DeletePreviewSoftNote, DeletePreviewAttachment)
    )

    assert preview.soft_deleted == {DeletePreviewSoftFolder: 1, DeletePreviewSoftNote: 1}
    assert preview.deleted == {DeletePreviewAttachment: 3}


@pytest.mark.asyncio
async def test_null_to_field_parent_cascades_to_no_row(db):
    """A parent whose nullable to_field= value is NULL is referenced by no row - its preview,
    soft delete and hard delete must leave every NULL-FK row alone."""
    russia = await NullableCodeCountry.objects.create(code="RU")
    moscow = await NullableCodeCity.objects.create(name="msk", country=russia)
    orphan_city = await NullableCodeCity.objects.create(name="orphan")
    orphan_shop = await NullableCodeShop.objects.create()
    russian_shop = await NullableCodeShop.objects.create(country=russia)
    no_code = await NullableCodeCountry.objects.create(code=None)

    preview = await no_code.delete_preview()
    assert preview.soft_deleted == {NullableCodeCountry: 1}
    assert preview.deleted == {}

    await no_code.delete()
    city_rows = await NullableCodeCity.objects.include_deleted().order_by("id").values("id", "deleted_at")
    assert city_rows == [{"id": moscow.id, "deleted_at": None}, {"id": orphan_city.id, "deleted_at": None}]

    await no_code.hard_delete()
    assert sorted(await NullableCodeShop.objects.all().values_list("id", flat=True)) == [
        orphan_shop.id,
        russian_shop.id,
    ]

    await russia.hard_delete()
    assert await NullableCodeShop.objects.all().values_list("id", flat=True) == [orphan_shop.id]
    assert await NullableCodeCity.objects.include_deleted().values_list("id", flat=True) == [orphan_city.id]
