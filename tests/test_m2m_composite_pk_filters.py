"""ManyToManyField.filter()'s `=`/`__not`/`__in`/`__not_in` lookups against a composite-PK
target (Finding 3) - previously get_m2m_filters() returned {} for one, so only bare equality
(via QuerySet._build_filter_q()'s own separate nested-Q expansion) worked at all."""

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
