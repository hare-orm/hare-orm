"""A custom Meta.manager filter only scopes ordinary reads: delete()/delete_preview()'s cascade and
PROTECT/RESTRICT checks and refresh_from_db() must still see every row that exists. Also covers
manager attributes inherited from an abstract base or a plain mixin."""

import os

import pytest
import pytest_asyncio

from hare import fields
from hare.contrib.test import truncate_all_models
from hare.contrib.test.isolated_contexts import hare_test_context
from hare.exceptions import DoesNotExist, IntegrityError, ProtectedError
from hare.fields.enums import OnDelete
from hare.models import Model
from hare.models.tenancy.tenancy import Tenancy
from hare.query.managers.manager import Manager


class ActiveOnlyManager(Manager):
    def get_queryset(self, **kwargs):
        return super().get_queryset(**kwargs).filter(active=True)


class FlagManager(Manager):
    def __init__(self, flag=None, model=None):
        super().__init__(model)
        self.flag = flag

    def get_queryset(self, **kwargs):
        queryset = super().get_queryset(**kwargs)
        return queryset.filter(type=self.flag) if self.flag else queryset


class ScopeParent(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=20)

    class Meta:
        table = "scope_parent"


class ScopeProtectChild(Model):
    id = fields.IntField(primary_key=True)
    parent = fields.ForeignKeyField("models.ScopeParent", related_name="protectors", on_delete=OnDelete.PROTECT)
    active = fields.BooleanField(default=True)
    all_objects = Manager()

    class Meta:
        table = "scope_protect_child"
        manager = ActiveOnlyManager()


class ScopeRestrictParent(Model):
    id = fields.IntField(primary_key=True)

    class Meta:
        table = "scope_restrict_parent"


class ScopeRestrictChild(Model):
    id = fields.IntField(primary_key=True)
    parent = fields.ForeignKeyField(
        "models.ScopeRestrictParent", related_name="restrictors", on_delete=OnDelete.RESTRICT, db_constraint=False
    )
    active = fields.BooleanField(default=True)

    class Meta:
        table = "scope_restrict_child"
        manager = ActiveOnlyManager()


class ScopeLooseParent(Model):
    id = fields.IntField(primary_key=True)

    class Meta:
        table = "scope_loose_parent"


class ScopeCascadeChild(Model):
    id = fields.IntField(primary_key=True)
    parent = fields.ForeignKeyField(
        "models.ScopeLooseParent", related_name="cascade_children", on_delete=OnDelete.CASCADE, db_constraint=False
    )
    active = fields.BooleanField(default=True)
    all_objects = Manager()

    class Meta:
        table = "scope_cascade_child"
        manager = ActiveOnlyManager()


class ScopeSetNullChild(Model):
    id = fields.IntField(primary_key=True)
    parent = fields.ForeignKeyField(
        "models.ScopeLooseParent",
        related_name="set_null_children",
        on_delete=OnDelete.SET_NULL,
        null=True,
        db_constraint=False,
    )
    active = fields.BooleanField(default=True)
    all_objects = Manager()

    class Meta:
        table = "scope_set_null_child"
        manager = ActiveOnlyManager()


class ScopeSetDefaultChild(Model):
    id = fields.IntField(primary_key=True)
    parent = fields.ForeignKeyField(
        "models.ScopeLooseParent",
        related_name="set_default_children",
        on_delete=OnDelete.SET_DEFAULT,
        default=999,
        db_constraint=False,
    )
    active = fields.BooleanField(default=True)
    all_objects = Manager()

    class Meta:
        table = "scope_set_default_child"
        manager = ActiveOnlyManager()


class ScopeSoftParent(Model):
    id = fields.IntField(primary_key=True)
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        table = "scope_soft_parent"
        soft_delete_field = "deleted_at"


class ScopeSoftChild(Model):
    id = fields.IntField(primary_key=True)
    parent = fields.ForeignKeyField("models.ScopeSoftParent", related_name="children", on_delete=OnDelete.CASCADE)
    active = fields.BooleanField(default=True)
    deleted_at = fields.DatetimeField(null=True)
    all_objects = Manager()

    class Meta:
        table = "scope_soft_child"
        manager = ActiveOnlyManager()
        soft_delete_field = "deleted_at"


class ScopeTenantParent(Model):
    id = fields.IntField(primary_key=True)
    company_id = fields.IntField()

    class Meta:
        table = "scope_tenant_parent"
        tenant_field = "company_id"


class ScopeTenantChild(Model):
    id = fields.IntField(primary_key=True)
    company_id = fields.IntField()
    parent = fields.ForeignKeyField(
        "models.ScopeTenantParent", related_name="children", on_delete=OnDelete.CASCADE, db_constraint=False
    )
    active = fields.BooleanField(default=True)
    all_objects = Manager()

    class Meta:
        table = "scope_tenant_child"
        manager = ActiveOnlyManager()
        tenant_field = "company_id"


class ScopeArticle(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=50)
    active = fields.BooleanField(default=True)
    all_objects = Manager()

    class Meta:
        table = "scope_article"
        manager = ActiveOnlyManager()


class ScopeTenantArticle(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=50)
    active = fields.BooleanField(default=True)
    company_id = fields.IntField()

    class Meta:
        table = "scope_tenant_article"
        manager = ActiveOnlyManager()
        tenant_field = "company_id"


class TypeBase(Model):
    id = fields.IntField(primary_key=True)
    type = fields.CharField(max_length=10)
    reds = FlagManager("red")

    class Meta:
        abstract = True


class TypeFromAbstract(TypeBase):
    class Meta:
        table = "scope_type_from_abstract"


class TypeFromAbstractSibling(TypeBase):
    class Meta:
        table = "scope_type_from_abstract_sibling"


class BlueManagerMixin:
    blues = FlagManager("blue")


class GreenManagerMixin(BlueManagerMixin):
    greens = FlagManager("green")


class TypeFromMixin(BlueManagerMixin, Model):
    id = fields.IntField(primary_key=True)
    type = fields.CharField(max_length=10)

    class Meta:
        table = "scope_type_from_mixin"


class TypeFromNestedMixin(GreenManagerMixin, Model):
    id = fields.IntField(primary_key=True)
    type = fields.CharField(max_length=10)

    class Meta:
        table = "scope_type_from_nested_mixin"


@pytest_asyncio.fixture(scope="module")
async def scope_context():
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    async with hare_test_context(
        modules=[__name__], db_url=db_url, app_label="models", connection_label="models"
    ) as ctx:
        yield ctx


@pytest_asyncio.fixture
async def scope_db(scope_context):
    yield scope_context
    await truncate_all_models()


@pytest.mark.asyncio
async def test_protect_sees_child_hidden_by_manager(scope_db):
    parent = await ScopeParent.objects.create(name="p")
    hidden_child = await ScopeProtectChild.objects.create(parent=parent, active=False)

    preview = await parent.delete_preview()
    assert not preview.can_delete
    assert [child.pk for child in preview.protected_by] == [hidden_child.pk]

    with pytest.raises(ProtectedError):
        await parent.delete()
    with pytest.raises(ProtectedError):
        await ScopeParent.objects.filter(pk=parent.pk).delete()
    assert await ScopeParent.objects.filter(pk=parent.pk).exists()


@pytest.mark.asyncio
async def test_restrict_sees_child_hidden_by_manager(scope_db):
    parent = await ScopeRestrictParent.objects.create()
    await ScopeRestrictChild.objects.create(parent=parent, active=False)

    preview = await parent.delete_preview()
    assert not preview.can_delete
    assert len(preview.restricted_by) == 1

    with pytest.raises(IntegrityError):
        await parent.delete()
    assert await ScopeRestrictParent.objects.filter(pk=parent.pk).exists()


@pytest.mark.asyncio
async def test_cascade_deletes_child_hidden_by_manager(scope_db):
    parent = await ScopeLooseParent.objects.create()
    await ScopeCascadeChild.objects.create(parent=parent, active=True)
    await ScopeCascadeChild.objects.create(parent=parent, active=False)

    preview = await parent.delete_preview()
    assert preview.deleted == {ScopeLooseParent: 1, ScopeCascadeChild: 2}

    await parent.delete()
    assert await ScopeCascadeChild.all_objects.all().count() == 0


@pytest.mark.asyncio
async def test_queryset_delete_cascades_to_child_hidden_by_manager(scope_db):
    parent = await ScopeLooseParent.objects.create()
    await ScopeCascadeChild.objects.create(parent=parent, active=False)

    await ScopeLooseParent.objects.filter(pk=parent.pk).delete()
    assert await ScopeCascadeChild.all_objects.all().count() == 0


@pytest.mark.asyncio
async def test_set_null_and_set_default_reach_child_hidden_by_manager(scope_db):
    parent = await ScopeLooseParent.objects.create()
    set_null_child = await ScopeSetNullChild.objects.create(parent=parent, active=False)
    set_default_child = await ScopeSetDefaultChild.objects.create(parent=parent, active=False)

    preview = await parent.delete_preview()
    assert preview.nulled == {ScopeSetNullChild: 1}
    assert preview.set_default == {ScopeSetDefaultChild: 1}

    await parent.delete()
    assert (await ScopeSetNullChild.all_objects.get(pk=set_null_child.pk)).parent_id is None
    assert (await ScopeSetDefaultChild.all_objects.get(pk=set_default_child.pk)).parent_id == 999


@pytest.mark.asyncio
async def test_soft_delete_cascade_reaches_child_hidden_by_manager(scope_db):
    parent = await ScopeSoftParent.objects.create()
    await ScopeSoftChild.objects.create(parent=parent, active=True)
    await ScopeSoftChild.objects.create(parent=parent, active=False)

    preview = await parent.delete_preview()
    assert preview.soft_deleted == {ScopeSoftParent: 1, ScopeSoftChild: 2}

    await parent.delete()
    deleted_at_values = await ScopeSoftChild.all_objects.all().include_deleted().values_list("deleted_at", flat=True)
    assert len(deleted_at_values) == 2
    assert all(deleted_at is not None for deleted_at in deleted_at_values)


@pytest.mark.asyncio
async def test_tenant_cascade_reaches_child_hidden_by_manager(scope_db):
    with Tenancy.scope(1):
        parent = await ScopeTenantParent.objects.create(company_id=1)
        await ScopeTenantChild.objects.create(company_id=1, parent=parent, active=False)

        preview = await parent.delete_preview()
        assert preview.deleted == {ScopeTenantParent: 1, ScopeTenantChild: 1}

        await parent.delete()
        assert await ScopeTenantChild.all_objects.all().count() == 0


@pytest.mark.asyncio
async def test_refresh_from_db_ignores_manager_filter(scope_db):
    article = await ScopeArticle.objects.create(title="x")
    await ScopeArticle.all_objects.filter(pk=article.pk).update(active=False, title="changed")

    await article.refresh_from_db()
    assert article.active is False
    assert article.title == "changed"

    article.title = "local"
    await article.refresh_from_db(fields=["title"])
    assert article.title == "changed"


@pytest.mark.asyncio
async def test_refresh_from_db_of_deleted_row_still_raises(scope_db):
    article = await ScopeArticle.objects.create(title="x", active=False)
    await ScopeArticle.all_objects.filter(pk=article.pk).delete()

    with pytest.raises(DoesNotExist):
        await article.refresh_from_db()


@pytest.mark.asyncio
async def test_refresh_from_db_ignores_manager_filter_but_keeps_tenant_isolation(scope_db):
    with Tenancy.scope(1):
        article = await ScopeTenantArticle.objects.create(title="x", company_id=1, active=False)
        await article.refresh_from_db()
        assert article.active is False
    with Tenancy.scope(2), pytest.raises(DoesNotExist):
        await article.refresh_from_db()


@pytest.mark.asyncio
async def test_refresh_from_db_with_empty_fields_is_a_no_op(scope_db):
    article = await ScopeArticle.objects.create(title="x")
    article.title = "local"

    await article.refresh_from_db(fields=[])
    assert article.title == "local"

    unsaved = ScopeArticle(title="unsaved")
    await unsaved.refresh_from_db(fields=())
    assert unsaved.title == "unsaved"


def get_manager(owner: type, name: str):
    """The manager object a class holds under ``name`` - reading the attribute gives a queryset."""
    return vars(owner)[name]


def test_manager_from_abstract_base_keeps_constructor_state():
    assert get_manager(TypeFromAbstract, "reds").flag == "red"
    assert get_manager(TypeFromAbstractSibling, "reds").flag == "red"
    assert get_manager(TypeFromAbstract, "reds") is not get_manager(TypeFromAbstractSibling, "reds")
    assert get_manager(TypeFromAbstract, "reds")._model is TypeFromAbstract
    assert get_manager(TypeFromAbstractSibling, "reds")._model is TypeFromAbstractSibling
    assert TypeFromAbstract.reds.model is TypeFromAbstract


def test_manager_from_plain_mixin_is_bound_per_model():
    assert get_manager(TypeFromMixin, "blues")._model is TypeFromMixin
    assert get_manager(TypeFromNestedMixin, "blues")._model is TypeFromNestedMixin
    assert get_manager(TypeFromNestedMixin, "greens")._model is TypeFromNestedMixin
    assert get_manager(TypeFromMixin, "blues") is not get_manager(TypeFromNestedMixin, "blues")
    assert get_manager(BlueManagerMixin, "blues")._model is None
    assert get_manager(TypeFromNestedMixin, "greens").flag == "green"


@pytest.mark.asyncio
async def test_inherited_managers_filter_their_own_model(scope_db):
    await TypeFromAbstract.objects.create(type="red")
    await TypeFromAbstract.objects.create(type="other")
    await TypeFromAbstractSibling.objects.create(type="red")
    await TypeFromMixin.objects.create(type="blue")
    await TypeFromMixin.objects.create(type="other")
    await TypeFromNestedMixin.objects.create(type="green")
    await TypeFromNestedMixin.objects.create(type="blue")

    assert await TypeFromAbstract.reds.all().count() == 1
    assert await TypeFromAbstractSibling.reds.all().count() == 1
    assert await TypeFromMixin.blues.all().count() == 1
    assert await TypeFromNestedMixin.greens.all().values_list("type", flat=True) == ["green"]
    assert await TypeFromNestedMixin.blues.all().values_list("type", flat=True) == ["blue"]
