"""Relation operations share one conversion of their explicit through values."""

import pytest

from hare import Model, fields
from hare.core.hare_context import HareContext
from hare.ddl.constraints import UniqueConstraint
from tests.contracts.constants import CONTRACT_APP, CONTRACT_CONNECTION
from tests.contracts.single_number_field import SingleNumberField


@pytest.mark.asyncio
@pytest.mark.parametrize("composite", [False, True], ids=["single-key", "composite-key"])
@pytest.mark.parametrize("operation", ["add", "set", "set-clear", "create", "restore"])
async def test_explicit_through_values_are_consumed_once(contract_database, monkeypatch, composite, operation):
    monkeypatch.setattr(SingleNumberField, "consumed", [])
    connection = contract_database.get_connection()
    keeps_unique_pairs = connection.features.supports_unique_constraints
    if operation == "restore" and not keeps_unique_pairs:
        pytest.skip("Restoring an existing through row requires a unique pair")

    class Owner(Model):
        number = fields.IntField(primary_key=not composite, generated=False)
        if composite:
            region = fields.IntField(default=3)
            pk = fields.CompositePrimaryKey("region", "number")
        peers = fields.ManyToManyField(f"{CONTRACT_APP}.Peer", through=f"{CONTRACT_APP}.Link")

    class Peer(Model):
        number = fields.IntField(primary_key=not composite, generated=False)
        if composite:
            region = fields.IntField(default=3)
            pk = fields.CompositePrimaryKey("region", "number")

    class Link(Model):
        owner = fields.ForeignKeyField(
            f"{CONTRACT_APP}.Owner", db_constraint=connection.features.supports_foreign_keys
        )
        peer = fields.ForeignKeyField(f"{CONTRACT_APP}.Peer", db_constraint=connection.features.supports_foreign_keys)
        amount = SingleNumberField(null=True)
        note = fields.IntField(default=8)
        deleted_at = fields.DatetimeField(null=True)

        class Meta:
            soft_delete_field = "deleted_at"
            constraints = (UniqueConstraint(fields=("owner", "peer")),) if keeps_unique_pairs else ()

    contract_database.register_live_models(
        [Owner, Peer, Link], app_label=CONTRACT_APP, connection_alias=CONTRACT_CONNECTION, managed=True
    )
    editor = connection.dialect.schema_editor_class(connection)
    for model in (Owner, Peer, Link):
        await editor.create_model(HareContext.require_current().apps.get_binding(model))
    owner = await Owner.objects.create(number=1)
    peer = await Peer.objects.create(number=2)
    if operation in {"restore", "set-clear"}:
        await owner.peers.add(peer, through_defaults={"amount": 9})
        if operation == "restore":
            await owner.peers.remove(peer)
    given = {"amount": iter([55])}
    if operation == "create":
        await owner.peers.create(number=4, through_defaults=given)
    elif operation == "set":
        await owner.peers.set(peer, through_defaults=given)
    elif operation == "set-clear":
        other = await Peer.objects.create(number=3)
        await owner.peers.set(peer, other, clear=True, through_defaults=given)
    else:
        await owner.peers.add(peer, through_defaults=given)
    assert SingleNumberField.consumed == [[55]]
    assert await Link.objects.order_by("amount").values_list("amount", "note") == (
        [(55, 8)] * (2 if operation == "set-clear" else 1)
    )
