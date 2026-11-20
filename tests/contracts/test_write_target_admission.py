"""Every deletion route admits the same queryset before reading or writing rows."""

import datetime

import pytest

from hare import Model, fields
from hare.contrib.test import assert_query_count
from hare.core.hare_context import HareContext
from hare.exceptions import QueryError

pytestmark = [pytest.mark.database_independent, pytest.mark.asyncio]


@pytest.mark.parametrize("file_database", [False, True], ids=["memory", "file"])
@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
@pytest.mark.parametrize("start", [0, 1], ids=["limit", "offset"])
@pytest.mark.parametrize(
    "operation",
    ["delete", "hard_delete", "restore", "restore_child", "delete_child", "delete_visible_child", "returning"],
)
async def test_deletion_routes_refuse_slices_before_sql(tmp_path, file_database, composite, start, operation):
    class Parent(Model):
        number = fields.IntField(primary_key=not composite, generated=False)
        region = fields.IntField(default=7)
        deleted_at = fields.DatetimeField(null=True)
        if composite:
            pk = fields.CompositePrimaryKey("region", "number")

        class Meta:
            soft_delete_field = "deleted_at"

    class Child(Model):
        number = fields.IntField(primary_key=not composite, generated=False)
        region = fields.IntField(default=7)
        deleted_at = fields.DatetimeField(null=True)
        parent = fields.ForeignKeyField("library.Parent", db_constraint=False, on_delete=fields.CASCADE)
        if composite:
            pk = fields.CompositePrimaryKey("region", "number")

        class Meta:
            soft_delete_field = "deleted_at"

    url = f"sqlite+aiosqlite://{tmp_path / 'rows.sqlite3'}" if file_database else "sqlite+aiosqlite://:memory:"
    async with HareContext() as context:
        await context.init({"connections": {"archive": url}, "apps": {}})
        context.register_live_models([Parent, Child], "library", connection_alias="archive")
        await context.generate_schemas()
        assert not context.get_connection("archive").features.supports_update_limit_order_by
        deleted_at = datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC) if operation.startswith("restore") else None
        for number in (1, 2):
            parent = await Parent.objects.create(number=number, deleted_at=deleted_at)
            await Child.objects.create(number=number, parent=parent, deleted_at=deleted_at)
        before = [await model.objects.include_deleted().order_by("number").values() for model in (Parent, Child)]
        model = Child if "child" in operation else Parent
        source = model.objects.using("archive").order_by("number")[start : start + 1]
        if operation == "delete_visible_child":
            source = source.include_deleted()
        if operation.startswith("restore"):
            query = source.restore(cascade=True)
        elif operation == "hard_delete":
            query = source.hard_delete()
        else:
            query = source.delete()
            if operation == "returning":
                query = query.returning("number")
        async with assert_query_count(0, using=context.get_connection("archive")):
            with pytest.raises(QueryError, match="slice"):
                await query
        assert [
            await model.objects.include_deleted().order_by("number").values() for model in (Parent, Child)
        ] == before
        # Explicitly empty queries remain no-ops, including after an otherwise refused slice.
        empty = source.none()
        async with assert_query_count(0, using=context.get_connection("archive")):
            if operation.startswith("restore"):
                assert await empty.restore(cascade=True) == 0
            elif operation == "hard_delete":
                assert await empty.hard_delete() == 0
            elif operation == "returning":
                assert await empty.delete().returning("number") == []
            else:
                assert await empty.delete() == 0


@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
@pytest.mark.parametrize("versioned", [False, True], ids=["ordinary", "optimistic-lock"])
async def test_update_or_create_preserves_the_row_outside_its_lookup(composite, versioned):
    class Owner(Model):
        id = fields.IntField(primary_key=True, generated=False)
        members = fields.ManyToManyField("library.Entry", related_name="owners")

    class Entry(Model):
        number = fields.IntField(primary_key=not composite, generated=False, source_field="stored_number")
        region = fields.IntField(default=7)
        if composite:
            pk = fields.CompositePrimaryKey("region", "number")
        owner = fields.ForeignKeyField(Owner, related_name="entries")
        label = fields.TextField()
        version = fields.IntField(default=0)

        class Meta:
            table = "upsert_entry"
            optimistic_lock_field = "version" if versioned else None

    async with HareContext() as context:
        await context.init({"connections": {"default": "sqlite+aiosqlite://:memory:"}, "apps": {}})
        context.register_live_models([Owner, Entry], "library", connection_alias="default")
        await context.generate_schemas()
        first_owner = await Owner.objects.create(id=1)
        second_owner = await Owner.objects.create(id=2)
        first = await Entry.objects.create(number=1, owner=first_owner, label="matched")
        second = await Entry.objects.create(number=2, owner=second_owner, label="untouched")
        await first_owner.members.add(first)
        await second_owner.members.add(second)

        connection = context.get_connection()
        untouched = "SELECT label, owner_id, version FROM upsert_entry WHERE stored_number = 2 AND region = 7"
        expected = [{"label": "untouched", "owner_id": 2, "version": 0}]
        assert await connection.execute_dicts(untouched) == expected

        for source in (Entry.objects, Entry.objects.filter(region=7), first_owner.entries, first_owner.members):
            for key in ({"pk": second.pk}, {"number": second.number}):
                try:
                    outcome = await source.update_or_create(pk=first.pk, defaults={"label": "changed", **key})
                except QueryError:
                    outcome = None
                assert await connection.execute_dicts(untouched) == expected
                if outcome is not None:
                    matched, created = outcome
                    assert (matched.pk, created) == (first.pk, False)

        same, created = await Entry.objects.update_or_create(
            pk=first.pk, defaults={"pk": first.pk, "label": "control"}
        )
        assert (same.pk, same.label, created) == (first.pk, "control", False)


async def test_preview_and_delete_null_only_the_matching_components():
    class Parent(Model):
        region = fields.IntField()
        number = fields.IntField()
        pk = fields.CompositePrimaryKey("region", "number")

        class Meta:
            table = "preview_parent"

    class Child(Model):
        id = fields.IntField(primary_key=True, generated=False)
        parent = fields.ForeignKeyField(Parent, null=True, on_delete=fields.SET_NULL)

        class Meta:
            table = "preview_child"

    async with HareContext() as context:
        await context.init({"connections": {"default": "sqlite+aiosqlite://:memory:"}, "apps": {}})
        context.register_live_models([Parent, Child], "library", connection_alias="default")
        await context.generate_schemas()
        first = await Parent.objects.create(region=1, number=1)
        second = await Parent.objects.create(region=1, number=2)
        await Child.objects.create(id=1, parent=first)
        await Child.objects.create(id=2, parent=second)
        await Child.objects.create(id=3, parent=None)
        connection = context.get_connection()
        children_sql = "SELECT id, parent_region, parent_number FROM preview_child ORDER BY id"
        baseline = [
            {"id": 1, "parent_region": 1, "parent_number": 1},
            {"id": 2, "parent_region": 1, "parent_number": 2},
            {"id": 3, "parent_region": None, "parent_number": None},
        ]
        assert await connection.execute_dicts(children_sql) == baseline
        preview = await first.delete_preview()
        assert preview.deleted == {Parent: 1}
        assert preview.nulled == {Child: 1}
        assert await connection.execute_dicts(children_sql) == baseline
        await first.delete()
        assert await connection.execute_dicts("SELECT region, number FROM preview_parent") == [
            {"region": 1, "number": 2}
        ]
        assert await connection.execute_dicts(children_sql) == [
            {"id": 1, "parent_region": None, "parent_number": None},
            *baseline[1:],
        ]
