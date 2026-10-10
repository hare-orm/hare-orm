"""ManyToManyField.filter()'s `=`/`__not`/`__in`/`__not_in` lookups against a composite-PK
target (Finding 3) - previously get_m2m_filters() returned {} for one, so only bare equality
(via PendingFilterCalls.build_filter_q(QuerySet)'s own separate nested-Q expansion) worked at all."""

import pytest

from hare.exceptions import UnSupportedError
from tests.testmodels import CompositePkM2MFilterOwner, CompositePkM2MFilterTarget


@pytest.mark.asyncio
async def test_composite_pk_m2m_equality_filter(db):
    owner = await CompositePkM2MFilterOwner.objects.create(name="Owner")
    target1 = await CompositePkM2MFilterTarget.objects.create(a=1, b=2, name="T1")
    target2 = await CompositePkM2MFilterTarget.objects.create(a=3, b=4, name="T2")
    await owner.peers.add(target1, target2)

    found = await CompositePkM2MFilterOwner.objects.filter(peers=target1)
    assert found == [owner]


@pytest.mark.asyncio
async def test_composite_pk_m2m_not_equal_filter(db):
    owner = await CompositePkM2MFilterOwner.objects.create(name="Owner")
    target1 = await CompositePkM2MFilterTarget.objects.create(a=1, b=2, name="T1")
    await owner.peers.add(target1)

    other = await CompositePkM2MFilterOwner.objects.create(name="Other")
    target2 = await CompositePkM2MFilterTarget.objects.create(a=3, b=4, name="T2")
    await other.peers.add(target2)

    found = await CompositePkM2MFilterOwner.objects.filter(peers__not=target1)
    assert found == [other]


@pytest.mark.asyncio
async def test_composite_pk_m2m_in_filter(db):
    owner1 = await CompositePkM2MFilterOwner.objects.create(name="Owner1")
    owner2 = await CompositePkM2MFilterOwner.objects.create(name="Owner2")
    target1 = await CompositePkM2MFilterTarget.objects.create(a=1, b=2, name="T1")
    target2 = await CompositePkM2MFilterTarget.objects.create(a=3, b=4, name="T2")
    target3 = await CompositePkM2MFilterTarget.objects.create(a=5, b=6, name="T3")
    await owner1.peers.add(target1)
    await owner2.peers.add(target3)

    found = await CompositePkM2MFilterOwner.objects.filter(peers__in=[target1, target2])
    assert found == [owner1]


@pytest.mark.asyncio
async def test_composite_pk_m2m_in_filter_accepts_raw_pk_tuples(db):
    owner = await CompositePkM2MFilterOwner.objects.create(name="Owner")
    target = await CompositePkM2MFilterTarget.objects.create(a=7, b=8, name="T")
    await owner.peers.add(target)

    found = await CompositePkM2MFilterOwner.objects.filter(peers__in=[(7, 8)])
    assert found == [owner]


@pytest.mark.asyncio
async def test_composite_pk_m2m_in_filter_empty_list(db):
    owner = await CompositePkM2MFilterOwner.objects.create(name="Owner")
    target = await CompositePkM2MFilterTarget.objects.create(a=1, b=2, name="T")
    await owner.peers.add(target)

    assert await CompositePkM2MFilterOwner.objects.filter(peers__in=[]) == []


@pytest.mark.asyncio
async def test_composite_pk_m2m_not_in_filter(db):
    owner1 = await CompositePkM2MFilterOwner.objects.create(name="Owner1")
    owner2 = await CompositePkM2MFilterOwner.objects.create(name="Owner2")
    target1 = await CompositePkM2MFilterTarget.objects.create(a=1, b=2, name="T1")
    target2 = await CompositePkM2MFilterTarget.objects.create(a=3, b=4, name="T2")
    await owner1.peers.add(target1)
    await owner2.peers.add(target2)

    found = await CompositePkM2MFilterOwner.objects.filter(peers__not_in=[target1])
    assert found == [owner2]


@pytest.mark.asyncio
async def test_composite_pk_m2m_not_in_filter_empty_list_matches_everything(db):
    owner = await CompositePkM2MFilterOwner.objects.create(name="Owner")
    target = await CompositePkM2MFilterTarget.objects.create(a=1, b=2, name="T")
    await owner.peers.add(target)

    assert await CompositePkM2MFilterOwner.objects.filter(peers__not_in=[]) == [owner]


@pytest.mark.asyncio
async def test_composite_pk_m2m_in_filter_rejects_none(db):
    with pytest.raises(UnSupportedError, match="None is not supported"):
        await CompositePkM2MFilterOwner.objects.filter(peers__in=[None])


@pytest.mark.asyncio
async def test_a_relation_to_a_composite_pk_compares_with_the_outer_row_key(db):
    """OuterReference("pk") of an outer row with a composite key names its key fields one by one - for a
    many-to-many relation to it, its `__pk`, and a backward relation from it, as for a foreign key."""
    from hare.query.expressions import Exists, OuterReference
    from tests.testmodels import CompositePkOwningFK, Tournament

    owner = await CompositePkM2MFilterOwner.objects.create(name="Owner")
    linked = await CompositePkM2MFilterTarget.objects.create(a=1, b=2, name="linked")
    await CompositePkM2MFilterTarget.objects.create(a=3, b=4, name="alone")
    await owner.peers.add(linked)
    for owners in (
        CompositePkM2MFilterOwner.objects.filter(peers=OuterReference("pk")),
        CompositePkM2MFilterOwner.objects.filter(peers__pk=OuterReference("pk")),
    ):
        targets = CompositePkM2MFilterTarget.objects.filter(Exists(owners))
        assert await targets.values_list("name", flat=True) == ["linked"]

    tournament = await Tournament.objects.create(name="cup")
    await Tournament.objects.create(name="other")
    await CompositePkOwningFK.objects.create(a=1, b=1, name="entry", tournament=tournament)
    entered = CompositePkOwningFK.objects.filter(
        Exists(Tournament.objects.filter(composite_owners=OuterReference("pk"), name="cup"))
    )
    assert await entered.values_list("name", flat=True) == ["entry"]


@pytest.mark.asyncio
async def test_a_many_to_many_relation_to_a_composite_pk_differs_from_the_outer_row_key(db):
    """`__not=OuterReference("pk")` keeps a row linked to another row or to none, as for a single-column key."""
    from hare.query.expressions import Exists, OuterReference

    first = await CompositePkM2MFilterTarget.objects.create(a=1, b=2, name="first")
    second = await CompositePkM2MFilterTarget.objects.create(a=3, b=4, name="second")
    only_first = await CompositePkM2MFilterOwner.objects.create(name="only first")
    await only_first.peers.add(first)
    await CompositePkM2MFilterOwner.objects.create(name="none")

    def names(owner_name):
        owners = CompositePkM2MFilterOwner.objects.filter(peers__not=OuterReference("pk"), name=owner_name)
        return CompositePkM2MFilterTarget.objects.filter(Exists(owners)).order_by("a").values_list("name", flat=True)

    assert await names("only first") == ["second"]
    assert await names("none") == ["first", "second"]
    assert second.pk == (3, 4)
