"""on_delete=PROTECT/SET_NULL on a plain (db_constraint=True, the default) ManyToManyField.

Unlike tests/deletion/test_hard_delete_unconstrained_cascade.py (db_constraint=False, no real FK
constraint at all), these models exercise the ordinary case: PROTECT is still checked in Python
before either a real hard DELETE or a soft-delete UPDATE runs (it has no SQL keyword of its own,
see ManyToManyFieldInstance.db_on_delete), while SET_NULL relies on the database's own real
ON DELETE SET NULL constraint action - the schema generator only needed to start emitting a
nullable column for it (see BaseSchemaGenerator._get_m2m_side_columns).
"""

import pytest

from hare import Connections
from hare.exceptions import ProtectedError
from tests.shared_models.testmodels import (
    Club,
    ClubMember,
    ClubMembership,
    CompositePkM2MProtectOwner,
    CompositePkM2MProtectPeer,
    HardDeleteAssociation,
    HardDeleteAssociationMember,
    HardDeleteAssociationMembership,
    HardDeleteUnconstrainedM2MPeer,
    HardDeleteUnconstrainedParent,
    M2MOnDeleteProtectParent,
    M2MOnDeleteProtectPeer,
    M2MOnDeleteSetNullParent,
    M2MOnDeleteSetNullPeer,
    SoftDeleteM2MPeer,
    SoftDeleteM2MSetNullParent,
)


@pytest.mark.parametrize(
    "pick_deleted",
    [
        pytest.param(lambda parent, peer: parent, id="m2m_protect_blocks_delete"),
        # on_delete is mirrored onto the auto-generated backward field (Apps.init_relations), so
        # PROTECT has to block from either side.
        pytest.param(lambda parent, peer: peer, id="m2m_protect_blocks_delete_from_generated_backward_field"),
    ],
)
@pytest.mark.asyncio
async def test_m2m_protect_blocks_delete(db, pick_deleted):
    parent = await M2MOnDeleteProtectParent.objects.create(name="P")
    peer = await M2MOnDeleteProtectPeer.objects.create(name="Peer")
    await parent.peers.add(peer)
    deleted = pick_deleted(parent, peer)

    with pytest.raises(ProtectedError):
        await deleted.delete()

    assert await type(deleted).objects.filter(pk=deleted.pk).exists()


@pytest.mark.asyncio
async def test_m2m_protect_allows_delete_once_unlinked(db):
    parent = await M2MOnDeleteProtectParent.objects.create(name="P")
    peer = await M2MOnDeleteProtectPeer.objects.create(name="Peer")
    await parent.peers.add(peer)
    await parent.peers.remove(peer)

    await parent.delete()

    assert not await M2MOnDeleteProtectParent.objects.filter(pk=parent.pk).exists()


@pytest.mark.asyncio
async def test_bulk_queryset_delete_respects_m2m_protect(db):
    parent = await M2MOnDeleteProtectParent.objects.create(name="P")
    peer = await M2MOnDeleteProtectPeer.objects.create(name="Peer")
    await parent.peers.add(peer)

    with pytest.raises(ProtectedError):
        await M2MOnDeleteProtectParent.objects.filter(pk=parent.pk).delete()

    assert await M2MOnDeleteProtectParent.objects.filter(pk=parent.pk).exists()


@pytest.mark.parametrize(
    ("parent_class", "peer_class", "field_name", "delete"),
    [
        # db_constraint=True (the default) means the through-table's FK constraint is real - a plain
        # hard DELETE relies entirely on the database's own ON DELETE SET NULL, no Python-side cascade
        # involved (ReverseRelationCascade.has_unconstrained_relations is False here).
        pytest.param(
            M2MOnDeleteSetNullParent,
            M2MOnDeleteSetNullPeer,
            "peers",
            lambda parent: parent.delete(),
            id="m2m_set_null_real_db_constraint_nulls_through_row",
        ),
        # db_constraint=False has no real FK constraint for the database to act on -
        # ReverseRelationCascade._set_null_m2m_through_rows reproduces it in Python, nulling this field's
        # own backward key column(s) instead of deleting the through-table row (unlike CASCADE).
        pytest.param(
            HardDeleteUnconstrainedParent,
            HardDeleteUnconstrainedM2MPeer,
            "nullable_peers",
            lambda parent: parent.delete(),
            id="m2m_set_null_nulls_through_row_when_constraint_absent",
        ),
        pytest.param(
            SoftDeleteM2MSetNullParent,
            SoftDeleteM2MPeer,
            "nullable_peers",
            lambda parent: parent.hard_delete(),
            id="m2m_set_null_hard_delete_nulls_out_through_row",
        ),
    ],
)
@pytest.mark.asyncio
async def test_m2m_set_null_nulls_the_through_row(db, parent_class, peer_class, field_name, delete):
    parent = await parent_class.objects.create(name="P")
    peer = await peer_class.objects.create(name="Peer")
    await getattr(parent, field_name).add(peer)

    field = parent_class._meta.fields_map[field_name]
    through_table = field.through
    backward_column = field.backward_keys[0]
    forward_column = field.forward_keys[0]

    await delete(parent)

    conn = Connections.get("models")
    rows = await conn.execute_dicts(f'SELECT * FROM "{through_table}"')
    assert len(rows) == 1
    assert rows[0][backward_column] is None
    assert rows[0][forward_column] == peer.pk


@pytest.mark.asyncio
async def test_bulk_queryset_delete_m2m_set_null_real_db_constraint(db):
    parent = await M2MOnDeleteSetNullParent.objects.create(name="P")
    peer = await M2MOnDeleteSetNullPeer.objects.create(name="Peer")
    await parent.peers.add(peer)

    field = M2MOnDeleteSetNullParent._meta.fields_map["peers"]
    through_table = field.through
    backward_column = field.backward_keys[0]

    deleted_count = await M2MOnDeleteSetNullParent.objects.filter(pk=parent.pk).delete()

    assert deleted_count == 1
    conn = Connections.get("models")
    rows = await conn.execute_dicts(f'SELECT * FROM "{through_table}"')
    assert len(rows) == 1
    assert rows[0][backward_column] is None


@pytest.mark.asyncio
async def test_composite_pk_m2m_protect_blocks_delete(db):
    """ReverseRelationCascade.check_protected's per-instance path - the composite-PK owner's own
    `_related_query`-equivalent (getattr(instance, field).all()) already handles a composite pk
    via the relation manager's own JOIN, unaffected by the bulk-only Q-per-row expansion below."""
    owner = await CompositePkM2MProtectOwner.objects.create(a=1, b=2, name="Owner")
    peer = await CompositePkM2MProtectPeer.objects.create(name="Peer")
    await owner.peers.add(peer)

    with pytest.raises(ProtectedError):
        await owner.delete()

    assert await CompositePkM2MProtectOwner.objects.filter(pk=(1, 2)).exists()


@pytest.mark.asyncio
async def test_composite_pk_m2m_protect_blocks_bulk_delete(db):
    """check_protected_bulk()'s composite-PK branch - no single `__in=` filter exists for a
    composite-PK model (get_m2m_filters() returns {} for one), so this ORs one equality Q per pk
    tuple instead."""
    owner1 = await CompositePkM2MProtectOwner.objects.create(a=1, b=2, name="Owner1")
    await CompositePkM2MProtectOwner.objects.create(a=3, b=4, name="Owner2")
    peer = await CompositePkM2MProtectPeer.objects.create(name="Peer")
    await owner1.peers.add(peer)

    with pytest.raises(ProtectedError):
        await CompositePkM2MProtectOwner.objects.filter(pk__in=[(1, 2), (3, 4)]).delete()

    assert await CompositePkM2MProtectOwner.objects.filter(pk=(1, 2)).exists()
    assert await CompositePkM2MProtectOwner.objects.filter(pk=(3, 4)).exists()


@pytest.mark.asyncio
async def test_composite_pk_m2m_protect_allows_delete_once_unlinked(db):
    owner = await CompositePkM2MProtectOwner.objects.create(a=5, b=6, name="Owner")
    peer = await CompositePkM2MProtectPeer.objects.create(name="Peer")
    await owner.peers.add(peer)
    await owner.peers.remove(peer)

    await owner.delete()

    assert not await CompositePkM2MProtectOwner.objects.filter(pk=(5, 6)).exists()


@pytest.mark.parametrize(
    ("owner_class", "member_class", "membership_class", "pick_deleted", "pick_kept", "nulled_column"),
    [
        # `Club.members`'s on_delete=SET_NULL is declared on the M2M field itself, with the through
        # model's own FK fields left at their CASCADE default - reconciled onto that FK field at
        # Apps.init_relations time, so deleting the club nulls the through row's `club` column instead
        # of the through model's own (unset) CASCADE deleting it outright.
        pytest.param(
            Club,
            ClubMember,
            ClubMembership,
            lambda owner, member: owner,
            lambda owner, member: {"member": member},
            "club_id",
            id="nulls_through_row",
        ),
        # From the related (member) side - deleting the member nulls the through row's `member`
        # column, leaving the row (and the club side) intact.
        pytest.param(
            Club,
            ClubMember,
            ClubMembership,
            lambda owner, member: member,
            lambda owner, member: {"club": owner},
            "member_id",
            id="from_related_side",
        ),
        # A model with no Meta.soft_delete_field at all - a real hard DELETE relies entirely on the
        # database's own ON DELETE SET NULL, which schema generation only gets right if the reconciled
        # on_delete already landed on the through model's own FK field before DDL was generated.
        pytest.param(
            HardDeleteAssociation,
            HardDeleteAssociationMember,
            HardDeleteAssociationMembership,
            lambda owner, member: owner,
            lambda owner, member: {"member": member},
            "association_id",
            id="real_db_constraint",
        ),
    ],
)
@pytest.mark.asyncio
async def test_m2m_through_model_on_delete_set_null(
    db, owner_class, member_class, membership_class, pick_deleted, pick_kept, nulled_column
):
    owner = await owner_class.objects.create(name="Owner")
    member = await member_class.objects.create(name="Member")
    await owner.members.add(member)

    await pick_deleted(owner, member).delete()

    row = await membership_class.objects.get(**pick_kept(owner, member))
    assert getattr(row, nulled_column) is None
