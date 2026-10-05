"""A read after a write of the same asyncio task goes to the connection the write went through, not
to the replica the router reads from - for the rest of the task, or for ``read_your_writes_seconds``;
``Routing.using_primary()`` sends every read there. A replica that never gets the writes stands in
for one that lags."""

import asyncio
import os
from contextlib import asynccontextmanager

import pytest

from hare.core.config import HareConfig
from hare.core.hare_context import HareContext
from hare.core.routing import Routing
from hare.dialects.base.connection.db_url_config_generator import DbUrlConfigGenerator
from hare.exceptions import ConfigurationError
from tests.read_your_writes_models import Note, Tag


class PrimaryReplicaRouter:
    def db_for_read(self, model):
        return "replica"

    def db_for_write(self, model):
        return "primary"


@asynccontextmanager
async def primary_and_replica(read_your_writes_seconds=None):
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    connections = {alias: DbUrlConfigGenerator.expand(db_url, testing=True) for alias in ("primary", "replica")}
    config = {
        "connections": connections,
        "apps": {"models": {"models": ["tests.read_your_writes_models"], "default_connection": "primary"}},
        "routers": [PrimaryReplicaRouter],
    }
    if read_your_writes_seconds is not None:
        config["read_your_writes_seconds"] = read_your_writes_seconds
    async with HareContext() as context:
        await context.init(config=config, _create_db=True)
        try:
            primary = context.connections.get("primary")
            await context.generate_schemas()
            await context.connections.get("replica").execute_script(primary.get_schema_sql(safe=True))
            yield context
        finally:
            await context.drop_databases()


async def read_texts():
    return await Note.objects.values_list("text", flat=True)


@pytest.mark.asyncio
async def test_a_read_after_a_write_goes_to_the_written_connection():
    async with primary_and_replica():

        async def request():
            assert await read_texts() == []
            await Note.objects.create(id=1, text="written")
            # The replica hasn't the row: the read goes to the primary written through.
            assert await read_texts() == ["written"]

        await asyncio.create_task(request())
        # Another task wrote nothing - it reads from the replica.
        assert await asyncio.create_task(read_texts()) == []


@pytest.mark.asyncio
async def test_two_tasks_keep_their_own_writes():
    async with primary_and_replica():
        written = asyncio.Event()

        async def writer():
            await Note.objects.create(id=1, text="written")
            written.set()
            return await read_texts()

        async def reader():
            await written.wait()
            return await read_texts()

        assert await asyncio.gather(writer(), reader()) == [["written"], []]


@pytest.mark.asyncio
async def test_the_reads_return_to_the_replica_after_the_window():
    async with primary_and_replica(read_your_writes_seconds=0.05):

        async def request():
            await Note.objects.create(id=1, text="written")
            assert await read_texts() == ["written"]
            await asyncio.sleep(0.1)
            assert await read_texts() == []
            Routing.forget_writes()

        await asyncio.create_task(request())


@pytest.mark.asyncio
async def test_using_primary_reads_from_the_written_connection():
    async with primary_and_replica():
        await asyncio.create_task(Note.objects.create(id=1, text="written"))
        assert await asyncio.create_task(read_texts()) == []

        async def request():
            async with Routing.using_primary():
                assert await read_texts() == ["written"]
            with Routing.using_primary():
                assert await read_texts() == ["written"]
            assert await read_texts() == []

        await asyncio.create_task(request())


async def written_in_a_task(write) -> list[str]:
    """Runs ``write`` in a task of its own and gives what a read right after it sees."""

    async def request():
        await write()
        return await read_texts()

    return await asyncio.create_task(request())


@pytest.mark.asyncio
async def test_every_write_sends_the_reads_to_the_written_connection():
    async with primary_and_replica():
        await Note.objects.using("primary").create(id=1, text="one")
        note = await Note.objects.using("primary").get(id=1)
        tag = await Tag.objects.using("primary").create(id=1, name="tag")
        # The rows above were written to set the test up, not by the tasks below.
        Routing.forget_writes()

        async def update():
            await Note.objects.filter(id=1).update(text="updated")

        async def bulk_create():
            await Note.objects.bulk_create([Note(id=2, text="two")])

        async def save():
            note.text = "saved"
            await note.save()

        async def delete():
            await Note.objects.filter(id=2).delete()

        async def link():
            await tag.notes.add(note)

        for write in (update, bulk_create, save, delete, link):
            # The replica has none of the primary's rows: a read from it sees nothing.
            assert await written_in_a_task(write) != [], write.__name__


@pytest.mark.asyncio
async def test_choosing_the_write_connection_writes_nothing():
    async with primary_and_replica():
        await Note.objects.using("primary").create(id=1, text="written")
        Routing.forget_writes()

        async def request():
            assert Note.get_connection(for_write=True).connection_alias == "primary"
            assert Note.objects.all().get_connection(for_write=True).connection_alias == "primary"
            return await read_texts()

        assert await asyncio.create_task(request()) == []


@pytest.mark.asyncio
async def test_a_write_made_before_the_routers_existed_changes_no_read():
    async with primary_and_replica() as context:
        routers = context.router._routers

        async def request():
            context.router._routers = None
            try:
                await Note.objects.using("primary").create(id=1, text="written")
            finally:
                context.router._routers = routers
            return await read_texts()

        assert await asyncio.create_task(request()) == []


@pytest.mark.parametrize("seconds", [0, -1, 3601, True, "5"])
def test_the_window_is_checked(seconds):
    with pytest.raises(ConfigurationError, match="read_your_writes_seconds"):
        HareConfig.load(
            {
                "connections": {"default": "sqlite+aiosqlite://:memory:"},
                "apps": {"models": {"models": ["tests.read_your_writes_models"]}},
                "read_your_writes_seconds": seconds,
            }
        )
