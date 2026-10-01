"""select_related() into a model whose columns' source_field isn't a plain lowercase name
("Some Col", "MixedCase") - the related row is keyed by field name and must hydrate by it."""

import pytest

from hare import Hare, fields
from hare.core.connections import Connections
from hare.models import Model


def build_models():
    target = type(
        "SourceCasingTarget",
        (Model,),
        {
            "__module__": __name__,
            "id": fields.IntField(primary_key=True),
            "spaced": fields.IntField(source_field="Some Col", null=True),
            "mixed": fields.IntField(source_field="MixedCase", null=True),
            "Meta": type("Meta", (), {"table": "source_casing_target"}),
        },
    )
    source = type(
        "SourceCasingSource",
        (Model,),
        {
            "__module__": __name__,
            "id": fields.IntField(primary_key=True),
            "target": fields.ForeignKeyField("sourcecasing.SourceCasingTarget", related_name="sources"),
            "Meta": type("Meta", (), {"table": "source_casing_source"}),
        },
    )
    return target, source


@pytest.mark.asyncio
async def test_select_related_hydrates_quoted_mixed_case_columns(db):
    alias = next(iter(Connections.current().db_config))
    connection = Connections.get(alias)
    await connection.execute_script("DROP TABLE IF EXISTS source_casing_source")
    await connection.execute_script("DROP TABLE IF EXISTS source_casing_target")
    await connection.execute_script(
        'CREATE TABLE source_casing_target (id INTEGER PRIMARY KEY, "Some Col" INTEGER, "MixedCase" INTEGER)'
    )
    await connection.execute_script(
        "CREATE TABLE source_casing_source (id INTEGER PRIMARY KEY, target_id INTEGER NOT NULL)"
    )
    target, source = build_models()
    Hare.register_live_models([target, source], app_label="sourcecasing", connection_alias=alias)
    try:
        created_target = await target.objects.create(id=1, spaced=7, mixed=8)
        await source.objects.create(id=1, target=created_target)

        rows = await source.objects.all().select_related("target")
        assert [(row.target.spaced, row.target.mixed) for row in rows] == [(7, 8)]

        only_rows = await source.objects.all().select_related("target").only("id", "target__spaced")
        assert [row.target.spaced for row in only_rows] == [7]
    finally:
        Hare.unregister_live_models([target, source])
        await connection.execute_script("DROP TABLE IF EXISTS source_casing_source")
        await connection.execute_script("DROP TABLE IF EXISTS source_casing_target")
