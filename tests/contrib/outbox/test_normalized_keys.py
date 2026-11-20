import pytest

from hare import Model, fields
from hare.contrib.outbox import ChangeCapture, ChangePayload, OutboxEvent
from hare.contrib.test import hare_test_context

pytestmark = pytest.mark.sqlite_only


class ContactEvent(OutboxEvent):
    pass


class Contact(Model):
    email = fields.EmailField(primary_key=True)
    title = fields.TextField()

    class Meta:
        track_dirty_fields = True
        change_capture = ChangeCapture(ContactEvent, payload=ChangePayload.BEFORE_AND_AFTER)


class EditionContact(Model):
    email = fields.EmailField()
    edition = fields.IntField()
    pk = fields.CompositePrimaryKey("email", "edition")
    title = fields.TextField()

    class Meta:
        track_dirty_fields = True
        change_capture = ChangeCapture(ContactEvent, payload=ChangePayload.BEFORE_AND_AFTER)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["save", "bulk_update", "delete"])
async def test_capture_matches_normalized_instance_keys(operation):
    async with hare_test_context([__name__], db_url="sqlite+aiosqlite://:memory:"):
        contact = await Contact.objects.create(email="Ann@EXAMPLE.COM", title="before")
        await ContactEvent.objects.all().delete()
        contact.email = "Ann@EXAMPLE.COM"
        contact.title = "after"
        match operation:
            case "save":
                await contact.save(update_fields=["title"])
            case "bulk_update":
                await Contact.objects.bulk_update([contact], fields=["title"])
            case "delete":
                await contact.delete()
        (event,) = await ContactEvent.objects.all()
        assert event.payload["before"]["title"] == "before"
        if operation == "delete":
            assert event.payload["after"] is None
        else:
            assert event.payload["after"]["title"] == "after"


@pytest.mark.asyncio
async def test_capture_tracks_a_normalized_component_of_a_moved_key():
    async with hare_test_context([__name__], db_url="sqlite+aiosqlite://:memory:"):
        await EditionContact.objects.create(email="old@example.com", edition=1, title="before")
        await ContactEvent.objects.all().delete()
        await EditionContact.objects.filter(pk=("old@example.com", 1)).update(email="New@EXAMPLE.COM", title="after")
        (event,) = await ContactEvent.objects.all()
        assert event.payload["pk"] == {"email": "New@example.com", "edition": 1}
        assert event.payload["before"]["email"] == "old@example.com"
        assert event.payload["after"]["email"] == "New@example.com"
        assert event.payload["after"]["title"] == "after"
