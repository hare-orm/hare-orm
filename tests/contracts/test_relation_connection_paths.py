"""Relation shapes follow saved connections and runtime routing for single and composite keys."""

import os
import sys
from collections import Counter
from itertools import permutations
from types import ModuleType

import pytest
import pytest_asyncio

from hare import Model, Prefetch, fields, prefetch_related_objects
from hare.contrib.test import capture_queries, requires_features
from hare.core.hare_context import HareContext
from hare.core.routing.written_connections import WrittenConnections
from hare.exceptions import FieldError, QueryError
from hare.models.tenancy.tenancy import Tenancy
from hare.query.expressions import Q
from hare.query.managers.manager import Manager
from hare.query.queryset.relations.many_to_many.linked_keys import LinkedKeys
from hare.sql.builder.tables.table import Table
from tests.relations.connection_rebinding_models import Author, Book, Edition, Note, Review
from tests.relations.recursive_connection_models import CompositeTreeNode, TreeNode
from tests.utils.multi_database_context import MultiDatabaseTestContext
from tests.utils.parent_connection_router import ParentConnectionRouter
from tests.utils.relation_query_checks import RelationQueryChecks


@pytest_asyncio.fixture
async def mutation_databases(monkeypatch):
    class VisibleMembers:
        def get_condition(self, model, visibility):
            return Q(visible=True)

    class Member(Model):
        region = fields.IntField(source_field="member_number")
        number = fields.IntField(source_field="member_region")
        pk = fields.CompositePrimaryKey("region", "number")
        tenant = fields.IntField()
        visible = fields.BooleanField()
        objects = Manager(scope=VisibleMembers())

        class Meta:
            tenant_field = "tenant"

    class Owner(Model):
        id = fields.IntField(primary_key=True, generated=False)
        tenant = fields.IntField()
        members = fields.ManyToManyField("models.Member", related_name="owners", db_constraint=False)

        class Meta:
            tenant_field = "tenant"

    module = ModuleType("tests._relation_mutation_scopes")
    module.__models__ = [Member, Owner]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    async with MultiDatabaseTestContext.open(
        os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:"),
        ["models", "replica"],
        apps={"models": {"models": [module.__name__], "default_connection": "models"}},
    ) as context:
        await context.generate_schemas()
        await context.connections.get("replica").execute_script(
            context.connections.get("models").get_schema_sql(safe=True)
        )
        for alias, tenant in (("models", 2), ("replica", 1)):
            await Owner.objects.using(alias).create(id=1, tenant=tenant)
            await Member.objects.using(alias).create(region=7, number=1, tenant=tenant, visible=alias == "replica")
        yield context, Owner, Member


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation", ("add-key", "add-instance", "set", "remove-key", "remove-instance", "clear", "set-empty")
)
async def test_many_to_many_mutations_keep_the_saved_source_connection_and_scopes(mutation_databases, operation):
    context, owner_model, member_model = mutation_databases
    relation_field = HareContext.require_current().apps.get_binding(owner_model).fields_map["members"]
    table = Table(relation_field.through, schema=relation_field.through_schema)
    adds = operation in ("add-key", "add-instance", "set")
    for alias in ("models",) if adds else ("models", "replica"):
        client = context.connections.get(alias)
        insert = (
            client.query_class.into(table)
            .columns(*relation_field.backward_keys, *relation_field.forward_keys)
            .insert(1, 7, 1)
        )
        await client.execute(*insert.get_parameterized_sql(client.sql_context))
    with Tenancy.scope(1):
        owner = await owner_model.objects.using("replica").get(id=1)
        member = await member_model.objects.using("replica").get(pk=(7, 1))
        match operation:
            case "add-key":
                await owner.members.add((7, 1))
            case "add-instance":
                await owner.members.add(member)
            case "set":
                await owner.members.set((7, 1))
            case "remove-key":
                await owner.members.remove((7, 1))
            case "remove-instance":
                await owner.members.remove(member)
            case "clear":
                await owner.members.clear()
            case "set-empty":
                await owner.members.set([])
        for alias, expected in (("models", 1), ("replica", int(adds))):
            client = context.connections.get(alias)
            sql, values = client.query_class.from_(table).select("*").get_parameterized_sql(client.sql_context)
            assert len(await client.execute_dicts(sql, values)) == expected


@pytest.mark.sqlite_only
@pytest.mark.asyncio
@pytest.mark.parametrize("routed", (False, True))
async def test_cascade_keeps_a_foreign_models_route_when_alias_matches_an_independent_client(monkeypatch, routed):
    class Routes:
        def db_for_read(self, model):
            return "third"

        def db_for_write(self, model):
            return "third"

    class Parent(Model):
        id = fields.IntField(primary_key=True, generated=False)

    class Child(Model):
        id = fields.IntField(primary_key=True, generated=False)
        parent = fields.ForeignKeyField("parents.Parent", related_name="children", db_constraint=False)

    apps = {}
    for label, models, alias in (("parents", [Parent], "models"), ("children", [Child], "replica")):
        module = ModuleType(f"tests._independent_{label}")
        module.__models__ = models
        monkeypatch.setitem(sys.modules, module.__name__, module)
        apps[label] = {"models": [module.__name__], "default_connection": alias}
    async with MultiDatabaseTestContext.open(
        os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:"),
        ["models", "replica", "third"],
        apps=apps,
        routers=[Routes] if routed else None,
    ) as context:
        await context.generate_schemas()
        target = context.connections.get("third" if routed else "replica")
        if routed:
            for alias in ("models", "replica"):
                await target.execute_script(context.connections.get(alias).get_schema_sql(safe=True))
        await Parent.objects.create(id=1)
        await Child.objects.bulk_create([Child(id=11, parent_id=1), Child(id=12, parent_id=1)])
        independent = target.create_independent_client()
        try:
            await independent.execute_script(
                "CREATE TEMP TABLE parent(id INTEGER PRIMARY KEY); "
                "CREATE TEMP TABLE child(id INTEGER PRIMARY KEY, parent_id INTEGER NOT NULL); "
                "INSERT INTO child VALUES(99,1)"
            )
            parent = await Parent.objects.using(independent).create(id=1)
            assert await parent.children.count() == 2
            preview = await parent.delete_preview(using=independent)
            assert preview.deleted.get(Child) == 2
            assert await target.execute_dicts("SELECT id FROM child ORDER BY id") == [{"id": 11}, {"id": 12}]
            assert await independent.execute_dicts("SELECT id FROM child") == [{"id": 99}]
        finally:
            await independent.close()


@pytest_asyncio.fixture
async def foreign_tenant_databases(monkeypatch):
    class Parent(Model):
        region = fields.IntField(source_field="parent_number")
        number = fields.IntField(source_field="parent_region")
        pk = fields.CompositePrimaryKey("region", "number")
        tenant = fields.IntField(source_field="parent_tenant")

        class Meta:
            tenant_field = "tenant"

    class Child(Model):
        id = fields.IntField(primary_key=True, generated=False)
        tenant = fields.IntField()
        parent = fields.ForeignKeyField("parents.Parent", related_name="children", null=True, db_constraint=False)

        class Meta:
            tenant_field = "tenant"

    for name, models in (("tests._foreign_tenant_parents", [Parent]), ("tests._foreign_tenant_children", [Child])):
        module = ModuleType(name)
        module.__models__ = models
        monkeypatch.setitem(sys.modules, name, module)
    async with MultiDatabaseTestContext.open(
        os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:"),
        ["models", "replica"],
        apps={
            "parents": {"models": ["tests._foreign_tenant_parents"], "default_connection": "models"},
            "children": {"models": ["tests._foreign_tenant_children"], "default_connection": "replica"},
        },
    ) as context:
        await context.generate_schemas()
        await context.connections.get("replica").execute_script(
            context.connections.get("models").get_schema_sql(safe=True)
        )
        with Tenancy.scope(1):
            parent = await Parent.objects.create(region=7, number=1)
        with Tenancy.scope(2):
            await Parent.objects.using("replica").create(region=7, number=1)
            other_tenant = await Parent.objects.create(region=7, number=2)
        yield context, Parent, Child, parent, other_tenant


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("create", "bulk-create", "save", "update", "reverse-create"))
async def test_relation_tenant_checks_read_the_targets_configured_database(foreign_tenant_databases, operation):
    context, parent_model, child_model, parent, other_tenant = foreign_tenant_databases
    with Tenancy.scope(1):
        match operation:
            case "create":
                await child_model.objects.create(id=10, parent=parent)
            case "bulk-create":
                await child_model.objects.bulk_create([child_model(id=10, parent=parent)])
            case "reverse-create":
                await parent.children.create(id=10)
            case "save" | "update":
                child = await child_model.objects.create(id=10)
                if operation == "save":
                    child.parent = parent
                    await child.save(update_fields=["parent"])
                else:
                    await child_model.objects.filter(id=10).update(parent=parent)
        assert await context.connections.get("replica").execute_dicts(
            "SELECT id, tenant, parent_region, parent_number FROM child"
        ) == [{"id": 10, "tenant": 1, "parent_region": 7, "parent_number": 1}]
        with pytest.raises(QueryError, match="tenant 2"):
            await child_model.objects.create(id=20, parent=other_tenant)
        assert await child_model.objects.count() == 1
        assert (await parent_model.objects.get(pk=(7, 1))).tenant == 1
    assert await context.connections.get("replica").execute_dicts("SELECT parent_tenant FROM parent") == [
        {"parent_tenant": 2}
    ]


@pytest.fixture(
    params=[
        (Author, "books", (" 1", " 2")),
        (Author, "selected_books", (" 1", " 2")),
        (Author, "selected_editions", ("", " extra")),
        (Edition, "reviews", (" 1", " 2")),
        (Edition, "readers", ("",)),
    ],
    ids=["reverse-fk", "m2m", "m2m-composite-target", "reverse-composite-fk", "m2m-composite-source"],
)
def relation_case(request):
    return request.param


@pytest_asyncio.fixture
async def relation_databases(request):
    async with MultiDatabaseTestContext.open(
        os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:"),
        ["models", "replica"],
        apps={
            "models": {
                "models": ["tests.relations.connection_rebinding_models"],
                "default_connection": "models",
            }
        },
    ) as context:
        await context.generate_schemas()
        await context.connections.get("replica").execute_script(
            context.connections.get("models").get_schema_sql(safe=True)
        )
        for alias in ("models", "replica"):
            author = await Author.objects.using(alias).create(id=1, name=alias)
            edition = await Edition.objects.using(alias).create(number=1, revision=2, name=alias)
            extra_edition = await Edition.objects.using(alias).create(number=1, revision=3, name=alias + " extra")
            for number in (1, 2):
                book = await Book.objects.using(alias).create(
                    id=number + (getattr(request, "param", 0) if alias == "replica" else 0),
                    name=f"{alias} {number}",
                    author=author,
                )
                await Review.objects.using(alias).create(id=number, name=f"{alias} {number}", edition=edition)
                await author.selected_books.add(book, using=alias)
            await author.selected_editions.add(edition, extra_edition, using=alias)
        yield context


@pytest_asyncio.fixture
async def routed_relation_databases(relation_databases):
    relation_databases.router.init_routers([ParentConnectionRouter])
    token = WrittenConnections.current.set(None)
    try:
        yield relation_databases
    finally:
        WrittenConnections.current.reset(token)
        relation_databases.router.init_routers([])


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("queryset", "instance"))
@pytest.mark.parametrize("relation_databases", [10], indirect=True, ids=["different-related-keys"])
async def test_cascade_deletion_keeps_related_rows_on_the_selected_source_database(relation_databases, operation):
    source = relation_databases.connections.get("replica")
    untouched = relation_databases.connections.get("models")
    tables = [
        HareContext.require_current().apps.get_binding(Author).db_table,
        HareContext.require_current().apps.get_binding(Book).db_table,
        HareContext.require_current().apps.get_binding(Author).fields_map["selected_books"].through,
        HareContext.require_current().apps.get_binding(Author).fields_map["selected_editions"].through,
    ]
    quote = untouched.dialect.literals.quote_identifier
    before = {table: await untouched.execute_dicts(f"SELECT * FROM {quote(table)}") for table in tables}
    if operation == "queryset":
        await Author.objects.using(source).filter(id=1).delete()
    else:
        author = await Author.objects.using(source).get(id=1)
        await author.delete()
    for table in tables:
        remaining = await untouched.execute_dicts(f"SELECT * FROM {quote(table)}")
        assert Counter(tuple(row.items()) for row in remaining) == Counter(tuple(row.items()) for row in before[table])
        assert await source.execute_dicts(f"SELECT * FROM {quote(table)}") == []


@pytest_asyncio.fixture
async def recursive_databases():
    async with MultiDatabaseTestContext.open(
        os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:"),
        ["models", "replica"],
        apps={
            "models": {
                "models": ["tests.relations.recursive_connection_models"],
                "default_connection": "models",
            }
        },
        routers=[ParentConnectionRouter],
    ) as context:
        await context.generate_schemas()
        await context.connections.get("replica").execute_script(
            context.connections.get("models").get_schema_sql(safe=True)
        )
        for model in (TreeNode, CompositeTreeNode):
            for alias in ("models", "replica"):
                parent = None
                for number in range(1, 4):
                    key = {"id": number} if model is TreeNode else {"number": number, "revision": 1}
                    parent = await model.objects.using(alias).create(**key, name=f"{alias}-{number}", parent=parent)
        token = WrittenConnections.current.set(None)
        try:
            yield context
        finally:
            WrittenConnections.current.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [TreeNode, CompositeTreeNode], ids=["single-key", "composite-key"])
@pytest.mark.usefixtures("recursive_databases")
async def test_recursive_relation_queries_preserve_runtime_connection_choice(model):
    parent = await model.objects.using("replica").get(pk=1 if model is TreeNode else (1, 1))
    query = parent.children.with_recursive("children", max_depth=3).order_by("name")
    pinned = query.using("replica")
    for _ in range(2):
        await RelationQueryChecks.check(query, ["replica-2", "replica-3"])
    token = ParentConnectionRouter.routed_models.set(frozenset({model}))
    try:
        await RelationQueryChecks.check(query, ["models-2", "models-3"])
        await RelationQueryChecks.check(pinned, ["replica-2", "replica-3"])
    finally:
        ParentConnectionRouter.routed_models.reset(token)
    await RelationQueryChecks.check(query, ["replica-2", "replica-3"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "related_model", "relation_name", "parent_field", "key"),
    [(Author, Book, "books", "author", 1), (Edition, Review, "reviews", "edition", (1, 2))],
    ids=["single-key", "composite-key"],
)
@pytest.mark.parametrize("streams", [False, True], ids=["await", "iterator"])
@pytest.mark.usefixtures("relation_databases")
async def test_nested_attribute_prefetches_accept_every_declaration_order(
    model, related_model, relation_name, parent_field, key, streams
):
    lookups = (
        Prefetch(relation_name, related_model.objects.only("name").order_by("name")[:1], to_attribute="picked"),
        Prefetch(f"picked__{parent_field}", model.objects.only("name"), to_attribute="loaded_parent"),
        f"picked__loaded_parent__{relation_name}",
    )
    for ordered_lookups in permutations(lookups):
        query = model.objects.filter(pk=key).prefetch_related(*ordered_lookups)
        objs = [obj async for obj in query.iterator(chunk_size=1)] if streams else await query
        assert len(objs) == 1
        assert [obj.name for obj in objs[0].picked] == ["models 1"]
        async with capture_queries(using="models") as captured:
            parent = objs[0].picked[0].loaded_parent
            assert parent.name == "models"
            assert sorted(obj.name for obj in getattr(parent, relation_name)) == ["models 1", "models 2"]
        assert captured.count == 0
    nested = (
        related_model.objects.only("name")
        .order_by("name")
        .prefetch_related(Prefetch(parent_field, model.objects.only("name"), to_attribute="loaded_parent"))
    )
    query = model.objects.filter(pk=key).prefetch_related(
        f"picked__loaded_parent__{relation_name}",
        Prefetch(relation_name, nested[:1], to_attribute="picked"),
    )
    objs = [obj async for obj in query.iterator(chunk_size=1)] if streams else await query
    assert [obj.name for obj in objs[0].picked] == ["models 1"]
    async with capture_queries(using="models") as captured:
        parent = objs[0].picked[0].loaded_parent
        assert sorted(obj.name for obj in getattr(parent, relation_name)) == ["models 1", "models 2"]
    assert captured.count == 0
    # Reusing the source still loads its declared attributes after the dependent request ran.
    assert {obj.loaded_parent.name for obj in await nested} == {"models"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "related_model", "relation_name", "parent_field", "key"),
    [(Author, Book, "books", "author", 1), (Edition, Review, "reviews", "edition", (1, 2))],
    ids=["single-key", "composite-key"],
)
async def test_prefetch_attributes_preserve_instance_state_and_relation_caches(
    relation_databases, model, related_model, relation_name, parent_field, key
):
    for source, target, relation, attribute in (
        (model, related_model, relation_name, "_saved_in_db"),
        (model, related_model, relation_name, f"_{relation_name}"),
        (related_model, model, parent_field, f"_{parent_field}"),
    ):
        async with capture_queries(using="models") as captured:
            with pytest.raises(QueryError, match="conflicts with the attribute"):
                source.objects.prefetch_related(Prefetch(relation, target.objects.none(), to_attribute=attribute))
        assert captured.count == 0
    parent = (
        await model.objects.filter(pk=key)
        .prefetch_related(Prefetch(relation_name, related_model.objects.none(), to_attribute="_alternate_children"))
        .get()
    )
    assert parent._alternate_children == []
    assert parent._saved_in_db is True
    parent.name = "updated"
    async with capture_queries(using="models") as captured:
        await parent.save(update_fields=["name"])
    assert not any(query.lstrip().upper().startswith("INSERT") for query in captured.queries)
    assert await model.objects.filter(pk=key).values_list("name", flat=True) == ["updated"]
    child = (
        await related_model.objects.filter(**{parent_field: key})
        .prefetch_related(Prefetch(parent_field, model.objects.none(), to_attribute="_alternate_parent"))
        .first()
    )
    assert child._alternate_parent is None
    assert (await getattr(child, parent_field)).pk == key


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [TreeNode, CompositeTreeNode], ids=["single-key", "composite-key"])
@pytest.mark.usefixtures("recursive_databases")
async def test_raw_extra_columns_preserve_model_state_and_connection(model):
    source = model.objects.using("replica")
    table = HareContext.require_current().apps.get_binding(model).db_table
    first_key = HareContext.require_current().apps.get_binding(model).primary_key_attribute_names[0]
    for name, value in (
        ("_saved_in_db", "0"),
        ("_connection_alias", "'models'"),
        ("_binding", "0"),
        ("_context", "0"),
        ("_declaration", "0"),
        ("save", "0"),
        ("_parent", "NULL"),
        ("_children", "NULL"),
    ):
        sql = f'SELECT source.*, {value} AS "{name}" FROM "{table}" source WHERE "{first_key}" = %s'
        for _ in range(2):
            with pytest.raises(FieldError, match="conflict with reserved attribute"):
                await source.raw(sql, [2])
    sql = (
        f'SELECT source.*, 99 AS "_raw_extra", 77 AS "_raw_extra_default" '
        f'FROM "{table}" source WHERE "{first_key}" = %s'
    )
    for _ in range(2):
        obj = (await source.raw(sql, [2]))[0]
        assert obj._raw_extra == 99 and obj._raw_extra_default == 77
        assert model._raw_extra_default == 0
        assert obj._connection_alias == "replica" and obj._saved_in_db is True
        obj.name = "updated"
        await obj.save(update_fields=["name"])
        assert await source.filter(pk=obj.pk).values_list("name", flat=True) == ["updated"]
        assert await model.objects.using("models").filter(pk=obj.pk).values_list("name", flat=True) == ["models-2"]
        assert await source.count() == 3


@pytest.mark.asyncio
async def test_raw_requires_every_component_of_a_composite_primary_key(recursive_databases):
    source = CompositeTreeNode.objects.using("replica")
    connection = recursive_databases.get_connection("replica")
    binding = recursive_databases.apps.get_binding(CompositeTreeNode)
    quote = connection.dialect.literals.quote_identifier
    table = quote(binding.db_table)
    for component in ("number", "revision"):
        with pytest.raises(FieldError, match="primary key"):
            await source.raw(f"SELECT {quote(component)}, name FROM {table}")
    result = await source.raw(f"SELECT name, revision, number FROM {table} ORDER BY number")
    assert [(obj.pk, obj.name) for obj in result] == [
        ((1, 1), "replica-1"),
        ((2, 1), "replica-2"),
        ((3, 1), "replica-3"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("clear", [False, True], ids=["keep-links", "replace-links"])
@pytest.mark.usefixtures("relation_databases")
async def test_many_to_many_set_keeps_a_single_composite_key_intact(clear):
    author = await Author.objects.using("models").get(pk=1)
    editions = await Edition.objects.using("models").order_by("pk")
    relation = author.selected_editions
    first, second = editions
    for members in (
        (first, second),
        (first.pk, second.pk),
        (first, second.pk),
        [first.pk, second.pk],
        (obj for obj in editions),
        Edition.objects.using("models").order_by("pk").values_list("pk", flat=True),
    ):
        await relation.set(members, clear=clear)
        assert sorted(await relation.values_list("pk", flat=True)) == [first.pk, second.pk]
        # async iteration fills the relation's fetched rows, which the next write invalidates.
        assert sorted([obj.pk async for obj in relation]) == [first.pk, second.pk]
        await relation.set(first.pk, clear=clear)
        assert await relation.values_list("pk", flat=True) == [first.pk]
    await relation.set((), clear=clear)
    assert await relation.count() == 0
    replica_author = await Author.objects.using("replica").get(pk=1)
    assert await replica_author.selected_editions.count() == 2


@pytest.mark.asyncio
@pytest.mark.usefixtures("relation_databases")
async def test_relation_queries_follow_a_parent_saved_on_another_connection(relation_case):
    model, relation_name, suffixes = relation_case
    key = (1, 2) if model is Edition else 1
    parent = await model.objects.get(pk=key)
    await RelationQueryChecks.check(getattr(parent, relation_name), ["models" + suffix for suffix in suffixes])
    pinned = getattr(parent, relation_name).all().using("models")
    for alias in ("replica", "models"):
        await parent.save(using=alias)
        relation = getattr(parent, relation_name)
        await RelationQueryChecks.check(relation, [alias + suffix for suffix in suffixes])
        await RelationQueryChecks.check(pinned, ["models" + suffix for suffix in suffixes])


@pytest.mark.asyncio
@pytest.mark.usefixtures("routed_relation_databases")
async def test_relation_queries_consult_the_parent_router_when_executed(relation_case):
    model, relation_name, suffixes = relation_case
    key = (1, 2) if model is Edition else 1
    parent = await model.objects.using("replica").get(pk=key)
    query = getattr(parent, relation_name).values_list("name", flat=True)
    replica_names = sorted("replica" + suffix for suffix in suffixes)
    model_names = sorted("models" + suffix for suffix in suffixes)
    assert sorted(await query) == replica_names
    matching = getattr(parent, relation_name).filter(name__startswith="models")
    combined = query.union(query.none())
    pinned = query.using("replica")
    token = ParentConnectionRouter.routed_models.set(frozenset({model}))
    try:
        assert sorted(await query) == model_names
        assert await matching.count() == len(model_names)
        assert await matching.exists()
        assert sorted(await combined) == model_names
        relation = getattr(parent, relation_name)
        await RelationQueryChecks.check(relation, model_names)
        assert sorted(await pinned) == replica_names
        related = relation.model.objects.order_by("name")
        await prefetch_related_objects(
            [parent], Prefetch(relation_name, queryset=related[:1], to_attribute="first_related")
        )
        assert [obj.name for obj in parent.first_related] == model_names[:1]
        await prefetch_related_objects(
            [parent], Prefetch(relation_name, queryset=related.using("replica"), to_attribute="replica_related")
        )
        assert [obj.name for obj in parent.replica_related] == replica_names
    finally:
        ParentConnectionRouter.routed_models.reset(token)
    assert sorted(await query) == replica_names


@pytest.mark.asyncio
@requires_features(supports_insert_conflicts=True)
@pytest.mark.parametrize("relation_name", ("notes", "selected_books"))
@pytest.mark.parametrize("selection", ("alias", "client"))
@pytest.mark.parametrize("override", (False, True), ids=("stored-choice", "keyword-overrides-choice"))
async def test_every_related_write_preserves_the_declared_connection(
    relation_databases, relation_name, selection, override
):
    many_to_many = relation_name == "selected_books"
    parent_model, model = (Author, Book) if many_to_many else (Edition, Note)
    parent = await parent_model.objects.using("models").get(pk=1 if many_to_many else (1, 2))
    primary = relation_databases.connections.get("models")
    replica = relation_databases.connections.get("replica")
    source_alias = "models" if override else "replica"
    source = source_alias if selection == "alias" else relation_databases.connections.get(source_alias)
    arguments = {"using": replica} if override else {}
    for index, operation in enumerate(
        ("create", "get_or_create", "update_or_create", "add", "remove", "set", "clear")
    ):
        key = 100 + index
        linked = operation in ("remove", "clear")
        base = getattr(parent, relation_name)
        declared = base.using(source)
        if operation not in ("create", "get_or_create", "update_or_create"):
            for connection in (primary, replica):
                obj = await model.objects.using(connection).create(
                    id=key,
                    name="existing",
                    **({"author": parent} if many_to_many else {"edition": parent if linked else None}),
                )
                if many_to_many and linked:
                    await base.add(obj, using=connection)
        if many_to_many:
            before_primary = set(await LinkedKeys.get_linked_pk_values(base, primary))
            before_replica = set(await LinkedKeys.get_linked_pk_values(base, replica))
        else:
            before_primary = (
                await model.objects.using(primary)
                .order_by("id")
                .values_list("id", "edition_number", "edition_revision")
            )
        if operation == "create":
            await declared.create(id=key, name="created", **({"author": parent} if many_to_many else {}), **arguments)
        elif operation == "get_or_create":
            await declared.get_or_create(
                id=key, defaults={"name": "created", **({"author": parent} if many_to_many else {})}, **arguments
            )
        elif operation == "update_or_create":
            await declared.update_or_create(
                id=key, defaults={"name": "created", **({"author": parent} if many_to_many else {})}, **arguments
            )
        else:
            obj = await model.objects.using(replica).get(id=key)
            if operation == "clear":
                await declared.clear(**arguments)
            elif operation == "set":
                await declared.set([obj], **arguments)
            else:
                await getattr(declared, operation)(obj, **arguments)
        if many_to_many:
            assert set(await LinkedKeys.get_linked_pk_values(base, primary)) == before_primary
            expected = (
                set()
                if operation == "clear"
                else {(key,)}
                if operation == "set"
                else before_replica - {(key,)}
                if operation == "remove"
                else before_replica | {(key,)}
            )
            assert set(await LinkedKeys.get_linked_pk_values(base, replica)) == expected
        else:
            assert (
                await model.objects.using(primary)
                .order_by("id")
                .values_list("id", "edition_number", "edition_revision")
                == before_primary
            )
            written = await model.objects.using(replica).get(id=key)
            assert (written.edition_number, written.edition_revision) == (
                (None, None) if operation in ("remove", "clear") else parent.pk
            )
