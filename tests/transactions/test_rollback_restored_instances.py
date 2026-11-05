"""A rollback reverts the rows and puts the instances back as they were before the transaction: the key a
reverse relation's add() set on a related instance, and the values the database gave a written row - a
computed column, a db_default - read back into the instance."""

from types import SimpleNamespace

import pytest

from hare import fields, prefetch_related_objects
from hare.contrib.test import hare_test_context
from hare.core.connections.connections import Connections
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.dialects.base.results.statement_result import StatementResult
from hare.fields.database_default import DatabaseDefault
from hare.fields.generated_field import GeneratedField
from hare.models import Model
from hare.models.write.instance_snapshot import InstanceSnapshot
from hare.models.write.instance_writer import InstanceWriter
from hare.models.write.rollback_restores import RollbackRestores
from hare.transactions.transactions import Transactions

pytestmark = pytest.mark.sqlite_only

DB_URL = "sqlite+aiosqlite://:memory:"


class Shelf(Model):
    id = fields.IntField(primary_key=True)


class Item(Model):
    id = fields.IntField(primary_key=True)
    label = fields.CharField(max_length=20, default="item")
    shelf = fields.ForeignKeyField("models.Shelf", related_name="items", null=True)

    class Meta:
        track_dirty_fields = True


class Priced(Model):
    id = fields.IntField(primary_key=True)
    price = fields.IntField()
    quantity = fields.IntField()
    total = GeneratedField(expression=RawSQLTerm("price * quantity"), output_field=fields.IntField(), stored=True)
    flag = fields.IntField(db_default=7)


class RejectingComputed(Model):
    id = fields.IntField(primary_key=True)
    amount = fields.IntField()
    doubled = GeneratedField(expression=RawSQLTerm("amount * 2"), output_field=fields.IntField(), stored=True)

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name == "doubled" and value == 20:
            raise ValueError("returned value rejected")


class Undo(Exception):
    pass


@pytest.mark.asyncio
async def test_executed_unknown_count_does_not_take_the_no_statement_path(monkeypatch):
    async with hare_test_context([__name__], db_url=DB_URL):
        shelf = await Shelf.objects.create()
        no_statement = await InstanceWriter(Shelf, Shelf.get_connection()).execute_update(shelf, None)
        assert not no_statement.performed
        assert no_statement.row_count is None

        item = await Item.objects.create()
        item.label = "after"
        connection = Item.get_connection()
        execute = connection.execute
        statements = []

        async def execute_without_count(sql, *args, **kwargs):
            statements.append(sql)
            result = await execute(sql, *args, **kwargs)
            return StatementResult(None, result.rows, result.inserted_id, result.description)

        monkeypatch.setattr(connection, "execute", execute_without_count)
        await item.save(using=connection)
        assert len(statements) == 1
        assert statements[0].startswith("UPDATE")


@pytest.mark.parametrize("returning", [False, True])
@pytest.mark.asyncio
async def test_bulk_rollback_keeps_loaded_objects_saved_and_new_objects_unsaved(returning):
    async with hare_test_context([__name__], db_url=DB_URL):
        saved = await Item.objects.create(id=1)
        new = Item(id=2)
        saved.label = "after"
        objects = [saved, new]
        before = [InstanceSnapshot.capture_persistence(obj).values for obj in objects]
        with pytest.raises(Undo):
            async with Transactions.atomic():
                await Item.objects.bulk_create(
                    objects, batch_size=1, returning=returning, on_conflict=["id"], update_fields=["label"]
                )
                assert all(obj._saved_in_db for obj in objects)
                raise Undo
        assert [InstanceSnapshot.capture_persistence(obj).values for obj in objects] == before
        assert saved.get_dirty_fields() == {"label": ("item", "after")}


@pytest.mark.parametrize("insert", [False, True])
@pytest.mark.asyncio
async def test_save_rollback_restores_persistence_flags_and_dirty_baseline(insert):
    async with hare_test_context([__name__], db_url=DB_URL):
        item = Item(label="before") if insert else await Item.objects.create(label="before")
        item.label = "after"
        object.__setattr__(item, "_connection_alias", "previous")
        before = InstanceSnapshot.capture_persistence(item)
        with pytest.raises(Undo):
            async with Transactions.atomic() as connection:
                await item.save(using=connection)
                assert item._connection_alias == connection.connection_alias
                assert item._saved_in_db
                raise Undo
        assert InstanceSnapshot.capture_persistence(item).values == before.values
        if not insert:
            assert item.get_dirty_fields() == {"label": ("before", "after")}


@pytest.mark.asyncio
async def test_generated_setter_failure_restores_already_published_value_on_rollback():
    async with hare_test_context([__name__], db_url=DB_URL):
        item = await RejectingComputed.objects.create(amount=2)
        item.amount = 10
        with pytest.raises(ValueError, match="returned value rejected"):
            async with Transactions.atomic() as connection:
                await item.save(using=connection)
        assert item.doubled == 4
        assert (await RejectingComputed.objects.get(pk=item.pk)).amount == 2


@pytest.mark.parametrize("bulk", [True, False], ids=["bulk", "save each"])
@pytest.mark.asyncio
async def test_a_rolled_back_reverse_add_puts_the_key_back(bulk):
    """The key stayed on the instance, and its next save() wrote the link the rollback undid."""
    async with hare_test_context([__name__], db_url=DB_URL):
        shelf = await Shelf.objects.create(id=1)
        item = await Item.objects.create(id=1)
        with pytest.raises(Undo):
            async with Transactions.atomic():
                await shelf.items.add(item, bulk=bulk)
                raise Undo
        assert item.shelf_id is None
        assert await item.shelf is None
        assert item.get_dirty_fields() == {}
        item.label = "later"
        await item.save()
        assert (await Item.objects.get(id=1)).shelf_id is None


@pytest.mark.asyncio
async def test_a_rolled_back_write_puts_back_what_the_database_gave_the_row():
    async with hare_test_context([__name__], db_url=DB_URL):
        saved = await Priced.objects.create(id=1, price=2, quantity=3)
        saved.price = 10
        with pytest.raises(Undo):
            async with Transactions.atomic():
                await saved.save()
                assert saved.total == 30
                raise Undo
        assert saved.total == 6

        new = Priced(id=2, price=2, quantity=3)
        with pytest.raises(Undo):
            async with Transactions.atomic():
                await new.save()
                assert (new.total, new.flag) == (6, 7)
                raise Undo
        assert isinstance(new.flag, DatabaseDefault)
        assert "total" not in vars(new)
        await new.save()
        assert (await Priced.objects.get(id=2)).flag == 7


@pytest.mark.asyncio
async def test_rollback_restore_uses_the_write_client_when_the_alias_has_changed():
    async with hare_test_context([__name__], db_url=DB_URL):
        item = await Item.objects.create(id=1, label="before")
        handler = Connections.current()
        original = handler.get("default")
        with pytest.raises(Undo):
            async with Transactions.atomic() as transaction:
                item.label = "after"
                await Item.objects.filter(id=1).update(label="after")
                token = handler.set("default", original)
                try:
                    RollbackRestores.register_rollback_restore(item, transaction, "label", "before")
                finally:
                    handler.reset(token)
                raise Undo
        assert item.label == "before"
        assert (await Item.objects.get(id=1)).label == "before"
        assert not item._pending_rollback_restore_stack


def test_snapshot_restoration_attempts_all_fields_in_reverse_order():
    attempts = []
    first_error = ValueError("first field")
    second_error = ValueError("second field")

    class Instance:
        _meta = SimpleNamespace(soft_delete_field=None)

        def __setattr__(self, name, value):
            attempts.append((name, value))
            if name == "first":
                raise first_error
            if name == "second":
                raise second_error
            object.__setattr__(self, name, value)

    first = Instance()
    second = Instance()
    snapshots = [
        InstanceSnapshot(first, {"first": 1, "untouched": 2}),
        InstanceSnapshot(second, {"second": 3, "remaining": 4}),
    ]
    with pytest.raises(ExceptionGroup) as raised:
        InstanceSnapshot.restore_all(snapshots)
    assert raised.value.exceptions == (second_error, first_error)
    assert attempts == [("remaining", 4), ("second", 3), ("untouched", 2), ("first", 1)]
    assert first.untouched == 2
    assert second.remaining == 4


@pytest.mark.asyncio
async def test_relation_bulk_insert_invalidates_rows_fetched_after_query_creation():
    async with hare_test_context([__name__], db_url=DB_URL):
        shelf = await Shelf.objects.create(id=1)
        query = shelf.items.bulk_create([Item(id=1)])
        await prefetch_related_objects([shelf], "items")
        assert list(shelf.items) == []
        await query
        assert not shelf.items.relation_rows._fetched
        assert [obj.pk for obj in await shelf.items] == [1]


@pytest.mark.asyncio
async def test_relation_reset_uses_the_client_even_when_its_alias_is_rebound():
    async with hare_test_context([__name__], db_url=DB_URL):
        shelf = await Shelf.objects.create(id=1)
        relation = shelf.items
        handler = Connections.current()
        original = handler.get("default")
        with pytest.raises(Undo):
            async with Transactions.atomic() as transaction:
                await Item.objects.create(id=1, shelf=shelf)
                await prefetch_related_objects([shelf], "items")
                assert len(list(shelf.items)) == 1
                token = handler.set("default", original)
                try:
                    relation._reset_cache_on_rollback(transaction)
                finally:
                    handler.reset(token)
                raise Undo
        assert not relation.relation_rows._fetched
        assert not relation._rollback_reset_layers
        assert await shelf.items == []


@pytest.mark.asyncio
async def test_relation_rollback_restores_the_loaded_parent_after_its_key():
    async with hare_test_context([__name__], db_url=DB_URL):
        shelf = await Shelf.objects.create(id=1)
        await Item.objects.create(id=1, shelf=shelf)
        item = await Item.objects.select_related("shelf").get(id=1)
        previous_shelf = item.shelf
        with pytest.raises(Undo):
            async with Transactions.atomic() as transaction:
                await shelf.items.remove(item, using=transaction)
                assert item.shelf_id is None
                raise Undo
        assert item.shelf_id == shelf.id
        assert item.shelf is previous_shelf


@pytest.mark.asyncio
async def test_committed_relation_write_releases_its_reset_registration():
    async with hare_test_context([__name__], db_url=DB_URL):
        shelf = await Shelf.objects.create(id=1)
        relation = shelf.items
        async with Transactions.atomic() as transaction:
            await relation.create(id=1, using=transaction)
            assert relation._rollback_reset_layers
        assert not relation._rollback_reset_layers
