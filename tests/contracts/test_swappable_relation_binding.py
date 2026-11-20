from types import ModuleType
from uuid import UUID

import pytest

from hare import Model, fields
from hare.contrib.test.databases.temporary_databases import TemporaryDatabases
from hare.core.hare_context import HareContext
from hare.models import swappable
from tests.utils.database_under_test import DatabaseUnderTest


@pytest.mark.asyncio
@pytest.mark.parametrize("target_key", ["uuid", "renamed", "composite"])
@pytest.mark.parametrize("relation_type", ["ForeignKeyField", "OneToOneField"])
@pytest.mark.parametrize("composite_child", [False, True])
@pytest.mark.parametrize("indirect", [False, True])
@pytest.mark.parametrize("registration", ["configured", "live"])
async def test_swappable_relations_rebuild_key_columns_and_restore_outer_binding(
    target_key, relation_type, composite_child, indirect, registration
):
    driver = DatabaseUnderTest.get_driver()
    features = driver.get_client_classes()[0].features
    if (relation_type == "OneToOneField" or indirect) and not features.supports_unique_constraints:
        pytest.skip("The database does not enforce one-to-one uniqueness")
    if indirect and target_key == "composite":
        pytest.skip("A one-to-one primary key cannot target a composite primary key")

    class DefaultTarget(Model):
        id = fields.IntField(primary_key=True, generated=False)

        class Meta:
            swappable = "TARGET_MODEL"

    replacement_fields = {"__module__": __name__}
    if target_key == "composite":
        replacement_fields.update(
            region=fields.UUIDField(), number=fields.IntField(), pk=fields.CompositePrimaryKey("region", "number")
        )
        target_values = {"region": UUID(int=7), "number": 12}
        target_pk = (UUID(int=7), 12)
    else:
        name = "token" if target_key == "renamed" else "id"
        replacement_fields[name] = fields.UUIDField(primary_key=True)
        target_values = {name: UUID(int=7)}
        target_pk = UUID(int=7)
    replacement = type("ReplacementTarget", (Model,), replacement_fields)

    class TargetLink(Model):
        root = fields.OneToOneField(
            swappable("TARGET_MODEL"),
            primary_key=True,
            related_name="links",
            db_constraint=features.supports_foreign_keys,
        )

    parent_reference = "models.TargetLink" if indirect else swappable("TARGET_MODEL")

    class Child(Model):
        parent = getattr(fields, relation_type)(
            parent_reference,
            related_name="children",
            source_field="parent_key",
            db_constraint=features.supports_foreign_keys,
        )
        version = fields.IntField(default=1)
        if composite_child:
            pk = fields.CompositePrimaryKey("parent", "version")
        else:
            id = fields.IntField(primary_key=True, generated=False)

    module = ModuleType("swappable_relation_keys")
    module.__models__ = [DefaultTarget, replacement, Child, *([TargetLink] if indirect else [])]  # type: ignore[attr-defined]
    inner_module = ModuleType("swappable_live_relation_keys")
    inner_module.__models__ = [  # type: ignore[attr-defined]
        model for model in module.__models__ if registration == "configured" or model is not Child
    ]
    config = {"connections": {"default": DatabaseUnderTest.get_url()}, "apps": {"models": {"models": [module]}}}
    child_values = {} if composite_child else {"id": 1}
    async with TemporaryDatabases(
        {**config, "swappable": {"TARGET_MODEL": "models.DefaultTarget"}}, DatabaseUnderTest.get_url()
    ):
        parent = await DefaultTarget.objects.create(id=5)
        if indirect:
            parent = await TargetLink.objects.create(root=parent)
        await Child.objects.create(parent=parent, **child_values)
        async with TemporaryDatabases(
            {
                **config,
                "apps": {"models": {"models": [inner_module]}},
                "swappable": {"TARGET_MODEL": "models.ReplacementTarget"},
            },
            DatabaseUnderTest.get_url(),
        ) as inner:
            if registration == "live":
                inner.register_live_models([Child], "models")
                await inner.generate_schemas(safe=True)
            relation = HareContext.require_current().apps.get_binding(Child).fields_map["parent"]
            assert (
                HareContext.require_current().apps.get_binding(Child).fields_map[relation.source_fields[0]].field_type
                is UUID
            )
            expected_columns = ("parent_region", "parent_number") if target_key == "composite" else ("parent_key",)
            assert relation.db_column_names == expected_columns
            parent = await replacement.objects.create(**target_values)
            if indirect:
                parent = await TargetLink.objects.create(root=parent)
            obj = await Child.objects.create(parent=parent, **child_values)
            expected_pk = ((*target_pk, 1) if target_key == "composite" else (target_pk, 1)) if composite_child else 1
            assert obj.pk == expected_pk
            fetched = await Child.objects.select_related("parent").get(pk=expected_pk)
            assert fetched.parent.pk == target_pk
            if indirect:
                assert (await fetched.parent.root).pk == target_pk
        assert HareContext.require_current().apps.get_binding(Child).fields_map["parent_id"].field_type is int
        assert HareContext.require_current().apps.get_binding(Child).fields_map["parent"].db_column_names == (
            "parent_key",
        )
        expected_pk = (5, 1) if composite_child else 1
        assert (await Child.objects.select_related("parent").get(pk=expected_pk)).parent.pk == 5
