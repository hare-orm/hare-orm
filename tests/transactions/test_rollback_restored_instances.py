"""A rollback reverts the rows and puts the instances back as they were before the transaction: the key a
reverse relation's add() set on a related instance, and the values the database gave a written row - a
computed column, a db_default - read back into the instance."""

import pytest

from hare import fields
from hare.contrib.test import hare_test_context
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.fields.database_default import DatabaseDefault
from hare.fields.generated_field import GeneratedField
from hare.models import Model
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


class Undo(Exception):
    pass


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
