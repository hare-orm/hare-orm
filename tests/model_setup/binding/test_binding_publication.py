from collections import UserDict, deque
from dataclasses import dataclass
from enum import IntEnum
from threading import Lock
from types import ModuleType
from typing import Any

import pytest

from hare import Model, fields
from hare.core.apps.enum_member_checks import EnumMemberChecks
from hare.core.apps.model_binding_graph import ModelBindingGraph
from hare.core.hare_context import HareContext
from hare.ddl.conditions.exclusive_arc_condition import ExclusiveArcCondition
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.indexes.index import Index
from hare.exceptions import ConfigurationError, FieldError
from hare.models import swappable
from hare.models.deletion.cascade.deletion_graph import DeletionGraph
from hare.query.queryset.pending_calls.calls_before_setup import CallsBeforeSetup

pytestmark = [pytest.mark.database_independent, pytest.mark.asyncio]


@pytest.mark.parametrize("external", [False, True])
@pytest.mark.parametrize("composite", [False, True])
@pytest.mark.parametrize(
    ("relation_type", "hidden"),
    [
        ("ForeignKeyField", False),
        ("OneToOneField", False),
        ("ManyToManyField", False),
        ("ForeignKeyField", True),
        ("OneToOneField", True),
    ],
)
async def test_unregister_removes_all_reverse_edges_and_keeps_previous_metadata(
    relation_type, hidden, composite, external
):
    class Parent(Model):
        region = fields.IntField()
        number = fields.IntField()
        if composite:
            pk = fields.CompositePrimaryKey("region", "number")

    options = {"related_name": False if hidden else "children", "db_constraint": False}
    if relation_type != "ManyToManyField":
        options["source_field"] = "parent_key"

    class Child(Model):
        parent = getattr(fields, relation_type)(Parent, **options)

    module = ModuleType("unregister_relation_targets")
    module.__models__ = [Parent]  # type: ignore[attr-defined]
    apps = {"parents": {"models": [module]}}
    config = {"connections": {"default": "sqlite+aiosqlite://:memory:"}}
    async with HareContext() as outer:
        await outer.init({**config, "apps": apps}, connect=False)
        async with HareContext() as inner:
            await inner.init(
                {**config, "apps": {"external_parents": {"models": [module]}} if external else apps}, connect=False
            )
            inner.register_live_models([Child], "children")
            previous_meta = HareContext.require_current().apps.get_binding(Parent)
            previous_fields = dict(previous_meta.fields_map)
            previous_hidden = dict(previous_meta.hidden_backward_relations)
            inner.unregister_live_models([Child])
            assert DeletionGraph.get_backward_relations(HareContext.require_current().apps.get_binding(Parent)) == []
            assert previous_meta.fields_map == previous_fields
            assert previous_meta.hidden_backward_relations == previous_hidden
            assert "children" not in HareContext.require_current().apps.get_binding(Parent).fields_map
            assert not HareContext.require_current().apps.get_binding(Parent).hidden_backward_relations
            inner.register_live_models([Child], "children")
            if relation_type != "ManyToManyField":
                expected_columns = ("parent_region", "parent_number") if composite else ("parent_key",)
                assert (
                    HareContext.require_current().apps.get_binding(Child).fields_map["parent"].db_column_names
                    == expected_columns
                )


@pytest.mark.parametrize("composite", [False, True])
@pytest.mark.parametrize(
    ("relation_type", "hidden"),
    [
        ("ForeignKeyField", False),
        ("OneToOneField", False),
        ("ManyToManyField", False),
        ("ForeignKeyField", True),
        ("OneToOneField", True),
    ],
)
async def test_failed_binding_restores_externally_referenced_models(relation_type, hidden, composite):
    class Parent(Model):
        region = fields.IntField()
        number = fields.IntField()
        if composite:
            pk = fields.CompositePrimaryKey("region", "number")

    class Descendant(Parent):
        pass

    class FirstChild(Model):
        parent = getattr(fields, relation_type)(
            Parent, related_name=False if hidden else "children", db_constraint=False
        )

    class SecondChild(Model):
        parent = getattr(fields, relation_type)(Parent, related_name="children", db_constraint=False)

    class ThirdChild(Model):
        parent = getattr(fields, relation_type)(Parent, related_name="children", db_constraint=False)

    parent_module = ModuleType("external_parent_binding")
    parent_module.__models__ = [Parent]  # type: ignore[attr-defined]
    child_module = ModuleType("external_child_binding")
    child_module.__models__ = [FirstChild, SecondChild, ThirdChild]  # type: ignore[attr-defined]
    config = {"connections": {"default": "sqlite+aiosqlite://:memory:"}}
    async with HareContext() as outer:
        await outer.init({**config, "apps": {"parents": {"models": [parent_module]}}}, connect=False)
        namespaces = {model: dict(vars(model)) for model in (Parent, Descendant)}
        previous_fields = dict(outer.apps.get_binding(Parent).fields_map)
        previous_hidden = dict(outer.apps.get_binding(Parent).hidden_backward_relations)
        async with HareContext() as inner:
            with pytest.raises(ConfigurationError, match="not registered"):
                await inner.init({**config, "apps": {"children": {"models": [child_module]}}}, connect=False)
            for model, attributes in namespaces.items():
                assert vars(model).keys() == attributes.keys()
                assert all(vars(model)[name] is value for name, value in attributes.items())
            assert outer.apps.get_binding(Parent).fields_map == previous_fields
            assert outer.apps.get_binding(Parent).hidden_backward_relations == previous_hidden


async def test_inheritance_after_binding_keeps_the_unexpanded_primary_key_declaration():
    class DefaultTarget(Model):
        region = fields.IntField()
        number = fields.IntField()
        pk = fields.CompositePrimaryKey("region", "number")

        class Meta:
            swappable = "TARGET_MODEL"

    class ReplacementTarget(Model):
        id = fields.UUIDField(primary_key=True)

    class Parent(Model):
        target = fields.ForeignKeyField(swappable("TARGET_MODEL"), related_name="%(class)s_parents")
        version = fields.IntField()
        pk = fields.CompositePrimaryKey("target", "version")

    module = ModuleType("bound_parent_declarations")
    module.__models__ = [DefaultTarget, ReplacementTarget, Parent]  # type: ignore[attr-defined]
    config = {"connections": {"default": "sqlite+aiosqlite://:memory:"}, "apps": {"binding": {"models": [module]}}}
    async with HareContext() as outer:
        await outer.init({**config, "swappable": {"TARGET_MODEL": "binding.DefaultTarget"}}, connect=False)

        class Descendant(Parent):
            pass

        child_module = ModuleType("inherited_parent_declarations")
        child_module.__models__ = [DefaultTarget, ReplacementTarget, Parent, Descendant]  # type: ignore[attr-defined]
        async with HareContext() as inner:
            await inner.init(
                {
                    **config,
                    "apps": {"binding": {"models": [child_module]}},
                    "swappable": {"TARGET_MODEL": "binding.ReplacementTarget"},
                },
                connect=False,
            )
            assert HareContext.require_current().apps.get_binding(Descendant).primary_key_attribute == (
                "target_id",
                "version",
            )
            assert isinstance(
                HareContext.require_current().apps.get_binding(Descendant).fields_map["target_id"], fields.UUIDField
            )
        assert HareContext.require_current().apps.get_binding(Parent).primary_key_attribute == (
            "target_region",
            "target_number",
            "version",
        )


@pytest.mark.parametrize("relation_type", ["ForeignKeyField", "OneToOneField", "ManyToManyField"])
@pytest.mark.parametrize("composite", [False, True])
async def test_rebinding_model_subsets_replaces_and_restores_backward_relations(relation_type, composite):
    class Parent(Model):
        region = fields.IntField()
        number = fields.IntField()

        if composite:
            pk = fields.CompositePrimaryKey("region", "number")

    class FirstChild(Model):
        parent = getattr(fields, relation_type)("binding.Parent", related_name="first_children")

    class SecondChild(Model):
        parent = getattr(fields, relation_type)("binding.Parent", related_name="second_children")

    first_module = ModuleType("first_model_subset")
    first_module.__models__ = [Parent, FirstChild]  # type: ignore[attr-defined]
    second_module = ModuleType("second_model_subset")
    second_module.__models__ = [Parent, SecondChild]  # type: ignore[attr-defined]
    async with HareContext() as outer:
        await outer.init(
            {
                "connections": {"default": "sqlite+aiosqlite://:memory:"},
                "apps": {"binding": {"models": [first_module]}},
            },
            connect=False,
        )
        for _ in range(2):
            async with HareContext() as inner:
                await inner.init(
                    {
                        "connections": {"default": "sqlite+aiosqlite://:memory:"},
                        "apps": {"binding": {"models": [second_module]}},
                    },
                    connect=False,
                )
                assert "first_children" not in HareContext.require_current().apps.get_binding(Parent).fields_map
                assert not hasattr(Parent, "first_children")
                assert (
                    HareContext.require_current().apps.get_binding(Parent).fields_map["second_children"].related_model
                    is SecondChild
                )
                if relation_type != "ManyToManyField":
                    forward = HareContext.require_current().apps.get_binding(SecondChild).fields_map["parent"]
                    assert all(
                        target is HareContext.require_current().apps.get_binding(Parent).fields_map[name]
                        for name, target in zip(forward.to_field_names, forward.to_field_instances, strict=True)
                    )
            assert "second_children" not in HareContext.require_current().apps.get_binding(Parent).fields_map
            assert not hasattr(Parent, "second_children")
            assert (
                HareContext.require_current().apps.get_binding(Parent).fields_map["first_children"].related_model
                is FirstChild
            )
            if relation_type != "ManyToManyField":
                forward = HareContext.require_current().apps.get_binding(FirstChild).fields_map["parent"]
                assert all(
                    target is HareContext.require_current().apps.get_binding(Parent).fields_map[name]
                    for name, target in zip(forward.to_field_names, forward.to_field_instances, strict=True)
                )


@pytest.mark.parametrize("relation_type", ["ForeignKeyField", "OneToOneField"])
async def test_rebinding_model_subsets_drops_and_restores_hidden_backward_relations(relation_type):
    class Parent(Model):
        pass

    class Child(Model):
        parent = getattr(fields, relation_type)("binding.Parent", related_name=False)

    full_module = ModuleType("models_with_hidden_relation")
    full_module.__models__ = [Parent, Child]  # type: ignore[attr-defined]
    parent_module = ModuleType("models_without_hidden_relation")
    parent_module.__models__ = [Parent]  # type: ignore[attr-defined]
    async with HareContext() as outer:
        await outer.init(
            {
                "connections": {"default": "sqlite+aiosqlite://:memory:"},
                "apps": {"binding": {"models": [full_module]}},
            },
            connect=False,
        )
        async with HareContext() as inner:
            await inner.init(
                {
                    "connections": {"default": "sqlite+aiosqlite://:memory:"},
                    "apps": {"binding": {"models": [parent_module]}},
                },
                connect=False,
            )
            assert not HareContext.require_current().apps.get_binding(Parent).hidden_backward_relations
        assert len(HareContext.require_current().apps.get_binding(Parent).hidden_backward_relations) == 1
        assert (
            next(
                iter(HareContext.require_current().apps.get_binding(Parent).hidden_backward_relations.values())
            ).related_model
            is Child
        )


@pytest.mark.parametrize("composite", [False, True])
@pytest.mark.parametrize("related_name", ["", "fixed_children", "%(app_label)s_%(class)s_children"])
@pytest.mark.parametrize("through", [None, "fixed_links"])
async def test_rebinding_many_to_many_names_uses_the_declaration(composite, related_name, through):
    class Parent(Model):
        region = fields.IntField()
        number = fields.IntField()
        if composite:
            pk = fields.CompositePrimaryKey("region", "number")

    class Child(Model):
        parent = fields.ManyToManyField("binding.Parent", related_name=related_name, through=through)

    module = ModuleType("automatic_backward_names")
    module.__models__ = [Parent, Child]  # type: ignore[attr-defined]
    config = {"connections": {"default": "sqlite+aiosqlite://:memory:"}, "apps": {"binding": {"models": [module]}}}
    fixed_name = "binding_child_children" if "%" in related_name else related_name
    async with HareContext() as outer:
        await outer.init(config, connect=False, table_name_generator=lambda model: f"outer_{model.__name__.lower()}")
        for _ in range(2):
            async with HareContext() as inner:
                await inner.init(
                    config, connect=False, table_name_generator=lambda model: f"inner_{model.__name__.lower()}"
                )
                actual = {
                    name
                    for name, field in HareContext.require_current().apps.get_binding(Parent).fields_map.items()
                    if getattr(field, "related_model", None) is Child
                }
                assert actual == {fixed_name or "inner_childs"}
                assert HareContext.require_current().apps.get_binding(Child).fields_map["parent"].through == (
                    through or "inner_child_inner_parent"
                )
            actual = {
                name
                for name, field in HareContext.require_current().apps.get_binding(Parent).fields_map.items()
                if getattr(field, "related_model", None) is Child
            }
            assert actual == {fixed_name or "outer_childs"}
            assert HareContext.require_current().apps.get_binding(Child).fields_map["parent"].through == (
                through or "outer_child_outer_parent"
            )


@pytest.mark.parametrize("composite", [False, True])
@pytest.mark.parametrize(
    ("outer_policy", "inner_policy"),
    [
        (fields.PROTECT, fields.RESTRICT),
        (fields.PROTECT, fields.CASCADE),
        (fields.CASCADE, fields.RESTRICT),
        (fields.RESTRICT, fields.PROTECT),
    ],
)
async def test_rebinding_through_models_preserves_declared_delete_rules(composite, outer_policy, inner_policy):
    class Parent(Model):
        region = fields.IntField()
        number = fields.IntField()
        if composite:
            pk = fields.CompositePrimaryKey("region", "number")

    class Child(Model):
        parent = fields.ManyToManyField("binding.Parent", through="binding.Link")

    def make_link(policy):
        class Link(Model):
            child = fields.ForeignKeyField("binding.Child", related_name=False, on_delete=policy)
            parent = fields.ForeignKeyField("binding.Parent", related_name=False)

        return Link

    outer_link, inner_link = make_link(outer_policy), make_link(inner_policy)
    outer_module, inner_module = ModuleType("outer_through_models"), ModuleType("inner_through_models")
    outer_module.__models__, inner_module.__models__ = [Parent, Child, outer_link], [Parent, Child, inner_link]
    async with HareContext() as outer:
        await outer.init(
            {
                "connections": {"default": "sqlite+aiosqlite://:memory:"},
                "apps": {"binding": {"models": [outer_module]}},
            },
            connect=False,
        )
        retained = HareContext.require_current().apps.get_binding(Child)
        assert retained.fields_map["parent"].on_delete == outer_policy
        async with HareContext() as inner:
            await inner.init(
                {
                    "connections": {"default": "sqlite+aiosqlite://:memory:"},
                    "apps": {"binding": {"models": [inner_module]}},
                },
                connect=False,
            )
            assert HareContext.require_current().apps.get_binding(Child).fields_map["parent"].on_delete == inner_policy
            assert (
                HareContext.require_current().apps.get_binding(inner_link).fields_map["child"].on_delete
                == inner_policy
            )
            assert retained.fields_map["parent"].on_delete == outer_policy
        assert HareContext.require_current().apps.get_binding(Child).fields_map["parent"].on_delete == outer_policy
        assert HareContext.require_current().apps.get_binding(outer_link).fields_map["child"].on_delete == outer_policy


@pytest.mark.parametrize("composite", [False, True])
@pytest.mark.parametrize("same_branch", [False, True])
@pytest.mark.parametrize("schema_objects", [False, True])
async def test_swappable_generic_relations_rebuild_branches_and_preserve_other_schema(
    composite, same_branch, schema_objects
):
    class Status(IntEnum):
        OPEN = 1
        CLOSED = 2

    class First(Model):
        region = fields.IntField()
        number = fields.IntField()
        if composite:
            pk = fields.CompositePrimaryKey("region", "number")

    class Second(Model):
        region = fields.IntField()
        number = fields.IntField()
        if composite:
            pk = fields.CompositePrimaryKey("region", "number")

    class Note(Model):
        marker = fields.IntField()
        status = fields.IntEnumField(Status)
        owner = fields.OneToOneField("binding.First", related_name="owned_note", null=True)
        fixed = fields.GenericForeignKeyField({"fixed_first": "binding.First"}, related_name="fixed_notes", null=True)
        target = fields.GenericForeignKeyField(swappable("TARGETS"), related_name="notes")

        class Meta:
            if schema_objects:
                indexes = (Index(fields=("target", "marker"), name="target_marker"),)
                constraints = (UniqueConstraint(fields=("target", "marker"), name="target_unique"),)

    module = ModuleType("swappable_generic_schema")
    module.__models__ = [First, Second, Note]
    config = {"connections": {"default": "sqlite+aiosqlite://:memory:"}, "apps": {"binding": {"models": [module]}}}
    old_branch, new_branch = ("item", "item") if same_branch else ("first", "second")
    async with HareContext() as outer:
        await outer.init({**config, "swappable": {"TARGETS": {old_branch: "binding.First"}}}, connect=False)
        retained = HareContext.require_current().apps.get_binding(Note)
        retained_fields = dict(retained.fields_map)
        retained_constraints = retained.constraints
        for _ in range(2):
            async with HareContext() as inner:
                await inner.init({**config, "swappable": {"TARGETS": {new_branch: "binding.Second"}}}, connect=False)
                assert Note.target.branch_names == (new_branch,)
                assert Note.target.branch_by_model == {Second: new_branch}
                assert (
                    HareContext.require_current().apps.get_binding(Note).fields_map[new_branch].related_model is Second
                )
                if not same_branch:
                    assert old_branch not in HareContext.require_current().apps.get_binding(Note).fields_map
                    assert not any(
                        name.startswith(f"{old_branch}_")
                        for name in HareContext.require_current().apps.get_binding(Note).fields_map
                    )
                arcs = [
                    constraint.check.fields
                    for constraint in HareContext.require_current().apps.get_binding(Note).constraints
                    if isinstance(getattr(constraint, "check", None), ExclusiveArcCondition)
                ]
                assert sorted(arcs) == sorted([("fixed_first",), (new_branch,)])
                assert (
                    sum(
                        EnumMemberChecks.is_enum_check(constraint)
                        for constraint in HareContext.require_current().apps.get_binding(Note).constraints
                    )
                    == 1
                )
                if composite:
                    assert any(
                        isinstance(constraint, UniqueConstraint)
                        and constraint.fields == ("owner_region", "owner_number")
                        for constraint in HareContext.require_current().apps.get_binding(Note).constraints
                    )
                if schema_objects:
                    assert [
                        index.fields for index in HareContext.require_current().apps.get_binding(Note).indexes
                    ] == [[new_branch, "marker"]]
                    assert any(
                        isinstance(constraint, UniqueConstraint) and constraint.fields == (new_branch, "marker")
                        for constraint in HareContext.require_current().apps.get_binding(Note).constraints
                    )
                assert retained.fields_map == retained_fields
                assert retained.constraints == retained_constraints
            assert Note.target.branch_by_model == {First: old_branch}
            assert HareContext.require_current().apps.get_binding(Note).fields_map[old_branch].related_model is First


async def test_failed_registration_preserves_metadata_and_subclass_attributes():
    class Parent(Model):
        value = fields.TextField()

    class Descendant(Parent):
        pass

    class FirstChild(Model):
        parent = fields.ForeignKeyField("binding.Parent", related_name="children")

    class SecondChild(Model):
        parent = fields.ForeignKeyField("binding.Parent", related_name="children")

    module = ModuleType("runtime_binding_publication")
    module.__models__ = [Parent]  # type: ignore[attr-defined]
    async with HareContext() as context:
        await context.init(
            {"connections": {"default": "sqlite+aiosqlite://:memory:"}, "apps": {"binding": {"models": [module]}}},
            connect=False,
        )
        models = (Parent, Descendant, FirstChild, SecondChild)
        previous_parent_binding = context.apps.get_binding(Parent)
        previous = {model: dict(vars(model)) for model in models}
        registry = context.apps.apps
        with pytest.raises(ConfigurationError, match="duplicates"):
            context.register_live_models([FirstChild, SecondChild], "binding")

        assert context.apps.apps is registry
        for model, attributes in previous.items():
            assert vars(model).keys() == attributes.keys()
            assert all(vars(model)[name] is value for name, value in attributes.items())
        assert context.apps._preparation is None

        context.register_live_models([FirstChild], "binding")
        previous_parent_meta = previous_parent_binding
        assert "children" not in previous_parent_meta.fields_map
        assert "children" in HareContext.require_current().apps.get_binding(Parent).fields_map
        relation = HareContext.require_current().apps.get_binding(FirstChild).fields_map["parent"]
        assert relation.to_field_instance is HareContext.require_current().apps.get_binding(Parent).fields_map["id"]
        assert HareContext.require_current().apps.get_binding(FirstChild).fields_map["parent_id"].reference is relation

        context.unregister_live_models([FirstChild])
        assert "children" not in HareContext.require_current().apps.get_binding(Parent).fields_map
        assert not hasattr(Descendant, "children")


async def test_reinitialization_preserves_the_original_graph_and_unbound_templates():
    class Existing(Model):
        value = fields.TextField()

    class Added(Model):
        value = fields.TextField()

    with HareContext():
        valid = Added.objects.filter(value="kept")
        invalid = Added.objects.filter(missing="bad")
    initial_module = ModuleType("initial_binding_publication")
    initial_module.__models__ = [Existing]
    replacement_module = ModuleType("replacement_binding_publication")
    replacement_module.__models__ = [Existing, Added]
    config = {
        "connections": {"default": "sqlite+aiosqlite://:memory:"},
        "apps": {"binding": {"models": [initial_module]}},
    }
    async with HareContext() as context:
        await context.init(config, connect=False)
        previous = (context.apps.binding_graph, context.apps, context.connections, context.router)
        replacement = {**config, "apps": {"binding": {"models": [replacement_module]}}}
        with pytest.raises(ConfigurationError, match="one application configuration"):
            await context.init(replacement, connect=False)
        assert all(
            current is original
            for current, original in zip(
                (context.apps.binding_graph, context.apps, context.connections, context.router), previous, strict=True
            )
        )
        assert valid._binding is None and invalid._binding is None
        async with HareContext() as other:
            await other.init(replacement, connect=False)
            bound = CallsBeforeSetup.bind(valid, other)
            assert bound._binding is other.apps.get_binding(Added)
            with pytest.raises(FieldError, match="missing"):
                CallsBeforeSetup.bind(invalid, other)
        assert valid._binding is None and valid._calls_before_setup is not None
        assert invalid._binding is None and invalid._calls_before_setup is not None


async def test_table_identity_includes_connection_schema_and_dialect_case_rules():
    class Item(Model):
        pass

    module = ModuleType("physical_binding_publication")
    module.__models__ = [Item]  # type: ignore[attr-defined]
    async with HareContext() as context:
        await context.init(
            {
                "connections": {
                    "default": "sqlite+aiosqlite://:memory:",
                    "other": "sqlite+aiosqlite://:memory:",
                },
                "apps": {"binding": {"models": [module], "default_connection": "default"}},
            },
            connect=False,
        )
        identity = context.apps.get_table_identity(Item, "Tenant", "Entries")
        assert identity == context.apps.get_table_identity(Item, "tenant", "entries")
        assert identity != context.apps.get_table_identity(Item, "another", "entries")
        async with HareContext() as other:
            await other.init(
                {
                    "connections": {"default": "sqlite+aiosqlite://:memory:", "other": "sqlite+aiosqlite://:memory:"},
                    "apps": {"binding": {"models": [module], "default_connection": "other"}},
                },
                connect=False,
            )
            assert identity != other.apps.get_table_identity(Item, "Tenant", "Entries")
        assert identity == context.apps.get_table_identity(Item, "Tenant", "Entries")


@pytest.mark.parametrize("wrapper", ("dict", "user-dict", "deque", "dataclass"))
async def test_declaration_snapshots_find_fields_and_callbacks_inside_custom_containers(wrapper):
    declaration_graph = ModelBindingGraph()

    class DefaultValue:
        def __init__(self):
            self.lock = Lock()

        def __call__(self):
            return []

        def __deepcopy__(self, memo):
            pytest.fail("A declaration snapshot must retain its callback")

    @dataclass
    class Configuration:
        field: Any

    default = DefaultValue()

    class Document(Model):
        value = fields.JSONField(default=default)

    field = declaration_graph.include(Document).fields_map["value"]
    root = fields.JSONField()
    if wrapper == "dict":
        root.configuration = {"field": field}
    elif wrapper == "user-dict":
        root.configuration = UserDict(field=field)
    elif wrapper == "deque":
        root.configuration = deque([field])
    else:
        root.configuration = Configuration(field)
    if wrapper in ("dict", "user-dict"):
        root.configuration["cycle"] = root.configuration
    snapshot = root.snapshot()
    configuration = snapshot.configuration
    if wrapper in ("dict", "user-dict"):
        copied_field = configuration["field"]
        assert configuration["cycle"] is configuration
    elif wrapper == "deque":
        copied_field = configuration[0]
    else:
        copied_field = configuration.field
    assert configuration is not root.configuration
    assert copied_field is not field
    assert copied_field.model is None
    assert copied_field.default is default
    assert field.model is Document


async def test_a_custom_container_field_keeps_its_nested_callback_when_published():
    declaration_graph = ModelBindingGraph()

    class DefaultValue:
        def __init__(self):
            self.lock = Lock()

        def __call__(self):
            return []

    class ConfiguredArrayField(fields.ArrayField):
        def __init__(self, base_field, **kwargs):
            self.configuration = UserDict()
            super().__init__(base_field, **kwargs)

        @property
        def base_field(self):
            return self.configuration["base_field"]

        @base_field.setter
        def base_field(self, value):
            self.configuration["base_field"] = value

    default = DefaultValue()

    class Document(Model):
        value = ConfiguredArrayField(fields.JSONField(default=default))

    assert declaration_graph.include(Document).fields_map["value"].base_field.default is default
