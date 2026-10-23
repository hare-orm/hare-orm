"""The tenant rules every write follows - a new row gets the scope's tenant and can't name another, rows move
inside the scope only, the writes of objects by key need a scope, and an upsert leaves another tenant's row
alone - whichever write it is."""

import pytest

from hare.contrib.test import requires_features
from hare.exceptions import QueryError
from hare.models.tenancy.tenancy import Tenancy
from tests.writes.rules.write_rules_models import RuleLabel, RuleTenantDocument, RuleTenantLabelLink, RuleTenantNote


async def create_object(company: str | None) -> None:
    await RuleTenantDocument.objects.create(id=1, title="a", **({"company": company} if company else {}))


async def save_new_object(company: str | None) -> None:
    await RuleTenantDocument(id=1, title="a", **({"company": company} if company else {})).save()


async def bulk_create_objects(company: str | None) -> None:
    await RuleTenantDocument.objects.bulk_create(
        [RuleTenantDocument(id=1, title="a", **({"company": company} if company else {}))]
    )


async def get_or_create_object(company: str | None) -> None:
    await RuleTenantDocument.objects.get_or_create(
        id=1, defaults={"title": "a", **({"company": company} if company else {})}
    )


@pytest.mark.parametrize("write", [create_object, save_new_object, bulk_create_objects, get_or_create_object])
@pytest.mark.asyncio
async def test_a_new_row_gets_the_tenant_of_the_scope(rules_db, write):
    with Tenancy.scope("a"):
        await write(None)
    assert await RuleTenantDocument.objects.all_tenants().values_list("company", flat=True) == ["a"]


@pytest.mark.parametrize("write", [create_object, save_new_object, bulk_create_objects, get_or_create_object])
@pytest.mark.asyncio
async def test_a_new_row_naming_a_tenant_outside_the_scope_is_refused(rules_db, write):
    with Tenancy.scope("a"), pytest.raises(QueryError):
        await write("b")
    assert await RuleTenantDocument.objects.all_tenants().count() == 0


async def update_rows(document: RuleTenantDocument) -> None:
    await RuleTenantDocument.objects.filter(id=document.id).update(company="b")


async def save_object(document: RuleTenantDocument) -> None:
    document.company = "b"
    await document.save()


async def bulk_update_objects(document: RuleTenantDocument) -> None:
    document.company = "b"
    await RuleTenantDocument.objects.bulk_update([document], ["company"])


async def upsert_objects(document: RuleTenantDocument) -> None:
    await RuleTenantDocument.objects.bulk_create(
        [RuleTenantDocument(id=document.id, company="b")], update_fields=["company"], on_conflict=["id"]
    )


@pytest.mark.parametrize("write", [update_rows, save_object, bulk_update_objects, upsert_objects])
@pytest.mark.asyncio
async def test_a_row_moved_out_of_the_scope_is_refused(rules_db, write):
    with Tenancy.scope("a"):
        document = await RuleTenantDocument.objects.create(id=1, title="a")
        with pytest.raises(QueryError):
            await write(document)
    assert await RuleTenantDocument.objects.all_tenants().values_list("company", flat=True) == ["a"]


async def bulk_create_unscoped() -> None:
    await RuleTenantDocument.objects.bulk_create([RuleTenantDocument(id=2, company="a")])


async def bulk_update_unscoped() -> None:
    document = await RuleTenantDocument.objects.all_tenants().get(id=1)
    document.title = "b"
    await RuleTenantDocument.objects.bulk_update([document], ["title"])


@pytest.mark.parametrize("write", [bulk_create_unscoped, bulk_update_unscoped])
@pytest.mark.asyncio
async def test_a_write_of_objects_by_key_needs_a_scope(rules_db, write):
    await RuleTenantDocument.objects.create(id=1, company="a", title="a")
    with pytest.raises(QueryError):
        await write()
    assert await RuleTenantDocument.objects.all_tenants().values_list("id", "title") == [(1, "a")]


@pytest.mark.asyncio
async def test_an_upsert_leaves_the_row_of_another_tenant_alone(rules_db):
    await RuleTenantDocument.objects.create(id=1, company="b", title="theirs")
    with Tenancy.scope("a"):
        document = RuleTenantDocument(id=1, title="mine")
        await RuleTenantDocument.objects.bulk_create([document], update_fields=["title"], on_conflict=["id"])
    assert (document.company, document._saved_in_db) == ("a", False)
    assert await RuleTenantDocument.objects.all_tenants().values_list("company", "title") == [("b", "theirs")]


@requires_features(supports_merge=True)
@pytest.mark.parametrize("branch", ["update", "insert"])
@pytest.mark.parametrize("by_object", [False, True], ids=["key column", "object"])
@pytest.mark.asyncio
async def test_a_merge_can_t_point_a_relation_at_a_row_of_another_tenant(rules_db, branch, by_object):
    """A merge writing the key column of a relation (document_id) wasn't checked - update() refuses it."""
    theirs = await RuleTenantDocument.objects.create(id=2, company="b")
    value = {"document": theirs} if by_object else {"document_id": 2}
    await RuleTenantNote.objects.create(id=1)
    with Tenancy.scope("a"):
        merge = (
            RuleTenantNote.objects.merge([{"id": 1}], on="id").when_matched(update=value)
            if branch == "update"
            else RuleTenantNote.objects.merge([{"id": 5}], on="id").when_not_matched(insert={"id": 5, **value})
        )
        with pytest.raises(QueryError, match="a relation can't cross tenants"):
            await merge
    assert await RuleTenantNote.objects.values_list("id", "document_id") == [(1, None)]


@requires_features(supports_merge=True)
@pytest.mark.asyncio
async def test_a_merge_of_every_tenant_writes_a_relation_as_given(rules_db):
    theirs = await RuleTenantDocument.objects.create(id=2, company="b")
    await RuleTenantNote.objects.create(id=1)
    with Tenancy.scope("a"):
        written = await (
            RuleTenantNote.objects.all_tenants().merge([{"id": 1}], on="id").when_matched(update={"document": theirs})
        )
    assert written == 1
    assert await RuleTenantNote.objects.values_list("document_id", flat=True) == [2]


@pytest.mark.parametrize(
    "scope",
    [lambda: Tenancy.scope("a"), lambda: Tenancy.scope(Tenancy.any_of("a", "c")), lambda: Tenancy.scope(Tenancy.ALL)],
)
@pytest.mark.parametrize("relink", ["add", "set"])
@pytest.mark.asyncio
async def test_a_link_naming_its_tenant_is_restored_from_its_soft_delete(rules_db, scope, relink):
    """Restoring the soft-deleted link wrote the tenant through_defaults named by update(), which refuses a
    tenant - under several tenants the only way to give the link its tenant."""
    label = await RuleLabel.objects.create(id=1, name="l")
    with scope():
        document = await RuleTenantDocument.objects.create(id=1, company="a")
        await document.labels.add(label, through_defaults={"id": 1, "company": "a"})
        await document.labels.remove(label)
        if relink == "add":
            await document.labels.add(label, through_defaults={"company": "a"})
        else:
            await document.labels.set([label], through_defaults={"company": "a"})
        links = await RuleTenantLabelLink.objects.all_tenants().include_deleted().values_list("id", "deleted_at")
    assert links == [(1, None)]


@pytest.mark.asyncio
async def test_a_partial_save_of_an_instance_of_another_tenant_is_refused(rules_db):
    """save(update_fields=...) not naming the tenant skipped the instance's tenant - a stale copy read under one
    tenant wrote into the row another tenant had moved to, which a full save() refuses."""
    with Tenancy.scope("a"):
        stale = await RuleTenantDocument.objects.create(id=1, company="a", title="a")
    with Tenancy.scope(Tenancy.any_of("a", "b")):
        await RuleTenantDocument.objects.filter(id=1).update(company="b")
    with Tenancy.scope("b"):
        stale.title = "stale"
        with pytest.raises(QueryError, match="a tenant other than the active one"):
            await stale.save(update_fields=["title"])
    assert await RuleTenantDocument.objects.all_tenants().values_list("company", "title") == [("b", "a")]
