"""UuidV7() on a PostgreSQL 18 server: every row gets a version 7 UUID, later rows greater ones; an older
server refuses the default before any DDL."""

from __future__ import annotations

import os
import uuid

import pytest

from hare import Model, fields
from hare.contrib.test import hare_test_context
from hare.core.connections.connections import Connections
from hare.exceptions import UnSupportedError
from hare.fields.db_defaults import UuidV7
from tests.dialects.postgresql.conftest import skip_if_not_postgres


class UuidV7Event(Model):
    id = fields.UUIDField(primary_key=True, db_default=UuidV7())
    name = fields.CharField(max_length=20)

    class Meta:
        app = "models"
        table = "uuid_v7_event"


@pytest.mark.asyncio
async def test_rows_get_time_ordered_version_7_uuids():
    skip_if_not_postgres()

    async with hare_test_context(
        modules=[__name__],
        db_url=os.environ["HARE_TEST_DB"],
        app_label="models",
        connection_label="models",
        _generate_schemas=False,
    ) as ctx:
        # Connected first, so the features follow the server's own version.
        async with ctx.get_connection().acquire_connection():
            pass
        if not ctx.get_connection().features.supports_uuid_v7:
            with pytest.raises(UnSupportedError, match="UuidV7"):
                await ctx.generate_schemas(safe=False)
            return
        await ctx.generate_schemas(safe=False)
        first = await UuidV7Event.objects.create(name="first")
        second = await UuidV7Event.objects.create(name="second")
        assert isinstance(first.id, uuid.UUID)
        assert first.id.version == 7
        assert second.id > first.id
        assert await UuidV7Event.objects.order_by("id").values_list("name", flat=True) == ["first", "second"]


@pytest.mark.asyncio
async def test_an_older_server_refuses_the_default(monkeypatch):
    skip_if_not_postgres()

    async with hare_test_context(
        modules=[__name__],
        db_url=os.environ["HARE_TEST_DB"],
        app_label="models",
        connection_label="models",
        _generate_schemas=False,
    ) as ctx:
        client = Connections.get("models")
        # Connected first: connecting sets the features of the server's version.
        async with client.acquire_connection():
            pass
        monkeypatch.setattr(client, "features", client.features.replace(supports_uuid_v7=False))
        with pytest.raises(UnSupportedError, match="UuidV7"):
            await ctx.generate_schemas(safe=False)
