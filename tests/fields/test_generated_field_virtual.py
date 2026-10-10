"""A stored=False (VIRTUAL) GeneratedField - computed on read, on SQLite and PostgreSQL 18+; an older
PostgreSQL server refuses it before any DDL."""

from __future__ import annotations

import pytest

from hare.contrib.test import requires_features
from hare.core.connections.connections import Connections
from hare.exceptions import UnSupportedError
from hare.fields.generated_field import GeneratedField
from tests.fields.models_generated_field_virtual import VirtualWidget


@pytest.mark.asyncio
async def test_generated_field_virtual(db_generated_field_virtual):
    widget = await VirtualWidget.objects.create(length=4, width=5)
    fetched = await VirtualWidget.objects.get(id=widget.id)
    assert fetched.area == 20
    assert await VirtualWidget.objects.filter(area__gt=10).count() == 1


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_an_older_server_refuses_a_virtual_column(db_generated_field_virtual, monkeypatch):
    client = Connections.get("models")
    monkeypatch.setattr(client, "features", client.features.replace(supports_virtual_generated_columns=False))
    field = VirtualWidget._meta.fields_map["area"]
    assert isinstance(field, GeneratedField)
    with pytest.raises(UnSupportedError, match="only supports STORED"):
        client.dialect.schema_editor_class(client).column_definitions.get_generated_column_sql(field)
