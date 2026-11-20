import sys
from types import ModuleType

import pytest

from hare import Model, fields
from hare.contrib.test import hare_test_context
from hare.fields.encrypted import EncryptedTextField
from hare.instrumentation.declarations import QueryExecuted
from hare.instrumentation.observers.observers import Observers
from tests.writes.rules.default_labels import DefaultLabels

pytestmark = pytest.mark.sqlite_only


class Book(Model):
    id = fields.IntField(primary_key=True)
    tags = fields.ManyToManyField("models.Tag", through="models.BookTag")


class Tag(Model):
    id = fields.IntField(primary_key=True)


class BookTag(Model):
    book = fields.ForeignKeyField("models.Book")
    tag = fields.ForeignKeyField("models.Tag")
    literal_label = EncryptedTextField(blind_index=True, null=True, default="literal label")
    called_label = EncryptedTextField(blind_index=True, null=True, default=DefaultLabels.get_label)
    awaited_label = EncryptedTextField(blind_index=True, null=True, default=DefaultLabels.get_async_label)
    note = fields.TextField(sensitive=True, default="private default note")


@pytest.mark.asyncio
async def test_through_model_defaults_write_blind_indexes_and_hide_parameters():
    async with hare_test_context([__name__], db_url="sqlite+aiosqlite://:memory:"):
        book = await Book.objects.create(id=1)
        tag = await Tag.objects.create(id=1)
        shown = []
        with Observers.observing(QueryExecuted, lambda event: shown.append(repr(event.parameters))):
            await book.tags.add(tag)
        assert shown
        assert all("private default note" not in parameters for parameters in shown)
        assert (
            await BookTag.objects.filter(
                literal_label="literal label", called_label="called label", awaited_label="awaited label"
            ).count()
            == 1
        )
        (link,) = await BookTag.objects.all()
        assert (link.literal_label, link.called_label, link.awaited_label) == (
            "literal label",
            "called label",
            "awaited label",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("add", "set"))
@pytest.mark.parametrize("asynchronous", (False, True))
async def test_given_through_values_do_not_evaluate_their_field_defaults(monkeypatch, operation, asynchronous):
    calls = []

    def rejected_default():
        calls.append("sync")
        raise RuntimeError("unused default")

    async def rejected_async_default():
        calls.append("async")
        raise RuntimeError("unused default")

    class Owner(Model):
        peers = fields.ManyToManyField("models.Peer", through="models.Link")

    class Peer(Model):
        pass

    class Link(Model):
        owner = fields.ForeignKeyField("models.Owner")
        peer = fields.ForeignKeyField("models.Peer")
        stamp = fields.IntField(default=rejected_async_default if asynchronous else rejected_default)
        note = fields.IntField(default=8)

    module = ModuleType("tests._through_given_values")
    module.__models__ = [Owner, Peer, Link]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    async with hare_test_context([module.__name__], db_url="sqlite+aiosqlite://:memory:"):
        owner = await Owner.objects.create()
        peer = await Peer.objects.create()
        if operation == "add":
            await owner.peers.add(peer, through_defaults={"stamp": 55})
        else:
            await owner.peers.set([peer], through_defaults={"stamp": 55})
        assert calls == []
        assert await Link.objects.values_list("stamp", "note") == [(55, 8)]
