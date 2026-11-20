"""Related writes retain the scope declared before choosing their model executor."""

import sys
from types import ModuleType

import pytest
import pytest_asyncio

from hare import Model, fields, prefetch_related_objects
from hare.contrib.test import hare_test_context, requires_features
from hare.exceptions import QueryError
from hare.models.tenancy.tenancy import Tenancy
from tests.utils.database_under_test import DatabaseUnderTest


@pytest_asyncio.fixture(params=(False, True), ids=("single-key", "composite-key"))
async def scoped_relations(request, monkeypatch):
    class Parent(Model):
        number = fields.IntField(primary_key=not request.param, generated=False)
        if request.param:
            revision = fields.IntField()
            pk = fields.CompositePrimaryKey("number", "revision")
        tenant = fields.IntField()

        class Meta:
            tenant_field = "tenant"

    class Child(Model):
        id = fields.IntField(primary_key=True, generated=False)
        tenant = fields.IntField()
        label = fields.TextField()
        deleted_at = fields.DatetimeField(null=True)
        parent = fields.ForeignKeyField("models.Parent", related_name="children", db_constraint=False)

        class Meta:
            tenant_field = "tenant"
            soft_delete_field = "deleted_at"

    class Tag(Model):
        area = fields.TextField()
        number = fields.IntField()
        pk = fields.CompositePrimaryKey("area", "number")
        tenant = fields.IntField()
        label = fields.TextField()
        deleted_at = fields.DatetimeField(null=True)
        parents = fields.ManyToManyField("models.Parent", related_name="tags", db_constraint=False)

        class Meta:
            tenant_field = "tenant"
            soft_delete_field = "deleted_at"

    module = ModuleType("tests._related_write_scopes")
    module.__models__ = [Parent, Child, Tag]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    async with hare_test_context([module.__name__], DatabaseUnderTest.get_url()):
        with Tenancy.scope(1):
            parent = await Parent.objects.create(number=7, **({"revision": 2} if request.param else {}))
        yield parent, Child, Tag


@pytest.mark.asyncio
@requires_features(supports_transactions=True)
@pytest.mark.parametrize("relation_name", ("children", "tags"), ids=("reverse", "m2m"))
@pytest.mark.parametrize("operation", ("get_or_create", "update_or_create"))
async def test_related_upserts_keep_declared_deleted_visibility(scoped_relations, relation_name, operation):
    parent, child_model, tag_model = scoped_relations
    model = child_model if relation_name == "children" else tag_model
    key = {"id": 1} if model is child_model else {"area": "", "number": 0}
    with Tenancy.scope(1):
        obj = await model.objects.create(**key, label="before", **({"parent": parent} if model is child_model else {}))
        if model is tag_model:
            await parent.tags.add(obj)
        await obj.delete()
        relation = getattr(parent, relation_name).include_deleted()
        result, created = await getattr(relation, operation)(**key, defaults={"label": "after"})
        assert not created
        expected_label = "after" if operation == "update_or_create" else "before"
        assert (result.pk, result.label) == (obj.pk, expected_label)
        assert await model.objects.include_deleted().values_list("label", flat=True) == [expected_label]
        assert await model.objects.count() == 0
        assert await relation.values_list("pk", flat=True) == [obj.pk]


@pytest.mark.asyncio
@requires_features(supports_transactions=True)
@pytest.mark.parametrize("operation", ("create", "bulk_create", "get_or_create", "update_or_create"))
async def test_related_writes_keep_all_tenants_without_an_ambient_scope(scoped_relations, operation):
    parent, child_model, _tag_model = scoped_relations
    values = {"id": 1, "tenant": 1, "label": "written"}
    relation = parent.children.all_tenants()
    if operation == "bulk_create":
        await relation.bulk_create([child_model(**values)])
    elif operation == "create":
        await relation.create(**values)
    else:
        result, created = await getattr(relation, operation)(id=1, defaults={"tenant": 1, "label": "written"})
        assert created
        assert result.pk == 1
    assert await child_model.objects.all_tenants().values_list("id", "tenant", "label") == [(1, 1, "written")]
    assert await parent.children.all_tenants().values_list("pk", flat=True) == [1]
    with pytest.raises(QueryError):
        await parent.children.count()
    assert await child_model.objects.all_tenants().count() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "expected"),
    [("include_deleted", ["deleted", "live"]), ("only_deleted", ["deleted"])],
)
async def test_visibility_clones_do_not_consume_the_instances_prefetched_rows(scoped_relations, method, expected):
    parent, child_model, _tag_model = scoped_relations
    with Tenancy.scope(1):
        await child_model.objects.create(id=1, label="live", parent=parent)
        deleted = await child_model.objects.create(id=2, label="deleted", parent=parent)
        await deleted.delete()
    with Tenancy.scope(1):
        await prefetch_related_objects([parent], "children")
        original = parent.children
        assert [obj.label for obj in original] == ["live"]
        declared = getattr(original, method)()
        assert sorted(obj.label for obj in await declared) == expected
        assert sorted([obj.label async for obj in declared]) == expected
        assert [obj.label for obj in original] == ["live"]
