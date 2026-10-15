"""``Tenancy.scope()`` with several values (``Tenancy.any_of``), every tenant (``Tenancy.ALL``) and
values given model by model: each model reads and writes inside its own part of the scope -
through relations, M2M, prefetches, bulk writes, cascades and restores too."""

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio

from hare.contrib.pydantic import pydantic_model_creator
from hare.contrib.test import requires_features, truncate_all_models
from hare.contrib.test.isolated_contexts import hare_test_context
from hare.core.hare_context import HareContext
from hare.exceptions import DoesNotExist, QueryError
from hare.models.tenancy.tenancy import Tenancy
from hare.query.functions import Count
from hare.query.relation_loading.prefetching.prefetch import Prefetch
from tests.tenant_scope_models import (
    ScopeCustomer,
    ScopeFaq,
    ScopeInvoice,
    ScopeLabel,
    ScopeOrder,
    ScopeOrderLabel,
    ScopeOrderLine,
    ScopePair,
    ScopePairNote,
    ScopeReceipt,
    ScopeSalesSummary,
    ScopeShop,
    ScopeShopOrder,
    ScopeTag,
)

STORES = ("msk", "kzn", "spb")
TWO_STORES = Tenancy.any_of("msk", "kzn")


def get_test_db_url() -> str:
    raw_db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:").replace("\\{", "{").replace("\\}", "}")
    return raw_db_url.format(uuid.uuid4().hex) if "{}" in raw_db_url else raw_db_url


@pytest_asyncio.fixture(scope="module")
async def tenant_scope_context() -> AsyncGenerator[Any]:
    async with hare_test_context(
        ["tests.tenant_scope_models"], db_url=get_test_db_url(), connection_label="models"
    ) as ctx:
        yield ctx


@pytest_asyncio.fixture
async def stores(tenant_scope_context: Any) -> AsyncGenerator[dict[str, ScopeOrder]]:
    """One order with two lines, a tag and a customer per store; summaries and FAQs of their own
    tenants."""
    orders = {}
    for position, store in enumerate(STORES, start=1):
        with Tenancy.scope({ScopeCustomer: "north" if store != "spb" else "south"}):
            customer = await ScopeCustomer.objects.create(id=position, name=f"customer-{store}")
        own_store = dict.fromkeys((ScopeOrder, ScopeOrderLine, ScopeTag, ScopePair), store)
        with Tenancy.scope(own_store):
            order = await ScopeOrder.objects.create(id=position, code=store, number=position, customer_id=customer.id)
            await ScopeOrderLine.objects.create(id=position * 10 + 1, order=order)
            await ScopeOrderLine.objects.create(id=position * 10 + 2, order=order)
            await ScopeTag.objects.create(id=position, name=f"tag-{store}")
            await ScopePair.objects.create(number=1, title=f"pair-{store}")
            orders[store] = order
        with Tenancy.scope(position):
            await ScopeSalesSummary.objects.create(id=position, total=position * 100)
        with Tenancy.scope(f"group-{position}"):
            await ScopeFaq.objects.create(id=position, question=f"question-{position}")
            await ScopeLabel.objects.create(id=position, name=f"label-{position}")
    yield orders
    await truncate_all_models()


async def get_stores(queryset: Any = None) -> list[str]:
    return sorted(await (queryset or ScopeOrder.objects.all()).values_list("store", flat=True))


async def get_all_order_rows() -> dict[int, tuple[str, int]]:
    rows = await ScopeOrder.objects.all_tenants().include_deleted().values_list("id", "store", "number")
    return {row[0]: (row[1], row[2]) for row in rows}


def test_any_of_needs_values():
    with pytest.raises(QueryError, match="needs at least one value"):
        Tenancy.any_of()
    with pytest.raises(ValueError, match="needs at least one value"):
        Tenancy.any_of()
    with pytest.raises(QueryError, match="None is not one"):
        Tenancy.any_of("msk", None)
    assert Tenancy.any_of("msk", "kzn", "msk").values == ("msk", "kzn")
    assert repr(TWO_STORES) == "Tenancy.any_of('msk', 'kzn')"
    assert repr(Tenancy.ALL) == "Tenancy.ALL"
    assert Tenancy.any_of("msk", "kzn") == TWO_STORES


def test_a_scope_by_model_names_tenant_models(tenant_scope_context):
    with pytest.raises(QueryError, match="is not a model with Meta.tenant_field"):
        Tenancy.set({ScopeShop: 1})
    with pytest.raises(QueryError, match="is not a model with Meta.tenant_field"):
        Tenancy.set({"ScopeOrder": "msk"})
    with pytest.raises(QueryError, match="ScopeOrder is given None"):
        Tenancy.set({ScopeOrder: None})
    for several_values in (["msk", "kzn"], ("msk", "kzn"), {"msk", "kzn"}, frozenset({"msk"})):
        with pytest.raises(QueryError, match=r"give several values as Tenancy.any_of\(\*values\)"):
            Tenancy.set(several_values)
        with pytest.raises(QueryError, match=r"ScopeOrder is given .* Tenancy.any_of\(\*values\)"):
            Tenancy.set({ScopeOrder: several_values})
    assert Tenancy.current.get() is None


def test_get_scope(tenant_scope_context):
    assert Tenancy.get_scope(ScopeOrder) is None
    with Tenancy.scope("msk"):
        assert Tenancy.get_scope(ScopeOrder) == "msk"
        assert Tenancy.get_scope(ScopeFaq) == "msk"
        assert Tenancy.get_scope(ScopeShop) is None
    with Tenancy.scope(TWO_STORES):
        assert Tenancy.get_scope(ScopeOrder) is TWO_STORES
    with Tenancy.scope(Tenancy.any_of("msk")):
        assert Tenancy.get_scope(ScopeOrder) == "msk"
    with Tenancy.scope(Tenancy.ALL):
        assert Tenancy.get_scope(ScopeOrder) is Tenancy.ALL
    with Tenancy.scope({ScopeOrder: TWO_STORES, ScopeSalesSummary: 2, ScopeFaq: Tenancy.ALL, ScopeTag: ["msk"][0]}):
        assert Tenancy.get_scope(ScopeOrder) is TWO_STORES
        assert Tenancy.get_scope(ScopeSalesSummary) == 2
        assert Tenancy.get_scope(ScopeFaq) is Tenancy.ALL
        assert Tenancy.get_scope(ScopeCustomer) is None
        with Tenancy.scope({ScopeCustomer: Tenancy.any_of("north")}):
            assert Tenancy.get_scope(ScopeCustomer) == "north"
            assert Tenancy.get_scope(ScopeOrder) is None
        with Tenancy.scope("kzn"):
            assert Tenancy.get_scope(ScopeSalesSummary) == "kzn"
        assert Tenancy.get_scope(ScopeOrder) is TWO_STORES
    assert Tenancy.get_scope(ScopeOrder) is None


def test_a_model_takes_the_entry_of_its_class_then_of_a_base_class(tenant_scope_context):
    with Tenancy.scope({ScopeInvoice.__mro__[1]: "msk"}):
        assert Tenancy.get_scope(ScopeInvoice) == "msk"
        assert Tenancy.get_scope(ScopeReceipt) == "msk"
    with Tenancy.scope({ScopeInvoice.__mro__[1]: "msk", ScopeReceipt: Tenancy.ALL}):
        assert Tenancy.get_scope(ScopeInvoice) == "msk"
        assert Tenancy.get_scope(ScopeReceipt) is Tenancy.ALL


@pytest.mark.parametrize(
    ("scope", "expected_stores"),
    [
        ("msk", ["msk"]),
        (TWO_STORES, ["kzn", "msk"]),
        (Tenancy.any_of("spb"), ["spb"]),
        (Tenancy.ALL, ["kzn", "msk", "spb"]),
        ({ScopeOrder: "kzn"}, ["kzn"]),
        ({ScopeOrder: TWO_STORES}, ["kzn", "msk"]),
        ({ScopeOrder: Tenancy.ALL}, ["kzn", "msk", "spb"]),
    ],
)
@pytest.mark.asyncio
async def test_reads_see_the_rows_of_the_scope(stores, scope, expected_stores):
    visible_ids = {stores[store].id for store in expected_stores}
    with Tenancy.scope(scope):
        assert await get_stores() == expected_stores
        assert await ScopeOrder.objects.count() == len(expected_stores)
        assert await ScopeOrder.objects.filter(number__gte=1).count() == len(expected_stores)
        assert await ScopeOrder.objects.aggregate(total=Count("id")) == {"total": len(expected_stores)}
        assert sorted(row["store"] for row in await ScopeOrder.objects.values("store")) == expected_stores
        for store, order in stores.items():
            assert await ScopeOrder.objects.filter(pk=order.pk).exists() is (order.id in visible_ids)
            if order.id in visible_ids:
                assert (await ScopeOrder.objects.get(pk=order.pk)).store == store
                assert (await ScopeOrder.objects.get(does_not_exist_exception=None, code=store)) is not None
            else:
                with pytest.raises(DoesNotExist):
                    await ScopeOrder.objects.get(pk=order.pk)
                assert await ScopeOrder.objects.get(does_not_exist_exception=None, code=store) is None
        assert await get_stores(ScopeOrder.objects.all_tenants()) == ["kzn", "msk", "spb"]


@pytest.mark.asyncio
async def test_a_model_the_scope_does_not_name_has_no_scope(stores):
    with Tenancy.scope({ScopeSalesSummary: 2}):
        assert await ScopeSalesSummary.objects.values_list("total", flat=True) == [200]
        with pytest.raises(QueryError, match=r"the active Tenancy.scope\(...\) doesn't name ScopeOrder"):
            await ScopeOrder.objects.all()
        with pytest.raises(QueryError, match="doesn't name ScopeOrder"):
            await ScopeOrder.objects.count()
        assert await ScopeOrder.objects.all_tenants().count() == 3
    with pytest.raises(QueryError, match="no tenant is active"):
        await ScopeOrder.objects.all()


@pytest.mark.asyncio
async def test_each_model_reads_its_own_part_of_the_scope(stores):
    scope = {ScopeOrder: TWO_STORES, ScopeSalesSummary: 2, ScopeFaq: Tenancy.ALL}
    with Tenancy.scope(scope):
        assert await get_stores() == ["kzn", "msk"]
        assert await ScopeSalesSummary.objects.values_list("store_id", flat=True) == [2]
        assert sorted(await ScopeFaq.objects.values_list("group", flat=True)) == ["group-1", "group-2", "group-3"]
    with Tenancy.scope({ScopeSalesSummary: Tenancy.any_of(1, 3), ScopeFaq: "group-2"}):
        assert sorted(await ScopeSalesSummary.objects.values_list("store_id", flat=True)) == [1, 3]
        assert await ScopeFaq.objects.values_list("question", flat=True) == ["question-2"]


@pytest.mark.asyncio
async def test_a_join_takes_the_scope_of_the_joined_model(stores):
    # kzn's and msk's customers are in the north, spb's in the south.
    with Tenancy.scope({ScopeOrder: Tenancy.ALL, ScopeCustomer: "north"}):
        assert await get_stores(ScopeOrder.objects.filter(customer__name__startswith="customer")) == ["kzn", "msk"]
        names = await ScopeOrder.objects.order_by("id").values_list("store", "customer__name")
        assert names == [("msk", "customer-msk"), ("kzn", "customer-kzn"), ("spb", None)]
        related = await ScopeOrder.objects.order_by("id").select_related("customer")
        assert [order.customer.name if order.customer else None for order in related] == [
            "customer-msk",
            "customer-kzn",
            None,
        ]
        prefetched = await ScopeOrder.objects.order_by("id").prefetch_related("customer")
        assert [order.customer.name if order.customer else None for order in prefetched] == [
            "customer-msk",
            "customer-kzn",
            None,
        ]
    with Tenancy.scope({ScopeOrder: "msk", ScopeCustomer: Tenancy.any_of("north", "south")}):
        customers = await ScopeCustomer.objects.filter(orders__number__gte=1).values_list("name", flat=True)
        assert customers == ["customer-msk"]
        counted = await ScopeCustomer.objects.annotate(order_count=Count("orders")).order_by("id")
        assert [customer.order_count for customer in counted] == [1, 0, 0]
        with_orders = await ScopeCustomer.objects.order_by("id").prefetch_related("orders")
        assert [[order.store for order in customer.orders] for customer in with_orders] == [["msk"], [], []]
    with Tenancy.scope({ScopeOrder: TWO_STORES}):
        with pytest.raises(QueryError, match="doesn't name ScopeCustomer"):
            await ScopeOrder.objects.filter(customer__name="customer-msk")
        assert await get_stores(ScopeOrder.objects.all_tenants().filter(customer__name="customer-spb")) == ["spb"]


@pytest.mark.asyncio
async def test_prefetches_and_reverse_relations_stay_in_scope(stores):
    with Tenancy.scope({ScopeOrder: TWO_STORES, ScopeOrderLine: "msk"}):
        orders = await ScopeOrder.objects.order_by("id").prefetch_related("lines")
        assert [(order.store, len(order.lines)) for order in orders] == [("msk", 2), ("kzn", 0)]
        explicit = await ScopeOrder.objects.order_by("id").prefetch_related(
            Prefetch("lines", queryset=ScopeOrderLine.objects.filter(quantity=1))
        )
        assert [len(order.lines) for order in explicit] == [2, 0]
        assert await stores["msk"].lines.all().count() == 2
        assert await stores["kzn"].lines.all().count() == 0
        assert await ScopeOrderLine.objects.filter(order__store="kzn").count() == 0
    with Tenancy.scope(TWO_STORES):
        assert await ScopeOrderLine.objects.filter(order__number__lte=3).count() == 4
        line = await ScopeOrderLine.objects.select_related("order").get(id=21)
        assert line.order.store == "kzn"


@pytest.mark.asyncio
async def test_create_takes_the_one_value_and_needs_a_named_one_otherwise(stores):
    with Tenancy.scope(TWO_STORES):
        with pytest.raises(QueryError, match="has no single value to give it"):
            await ScopeOrder.objects.create(id=10)
        assert (await ScopeOrder.objects.create(id=10, store="kzn")).store == "kzn"
        with pytest.raises(QueryError, match="does not match the active tenant scope"):
            await ScopeOrder.objects.create(id=11, store="spb")
    with Tenancy.scope(Tenancy.ALL):
        with pytest.raises(QueryError, match="has no single value to give it"):
            await ScopeOrder.objects.create(id=12)
        assert (await ScopeOrder.objects.create(id=12, store="ekb")).store == "ekb"
    with Tenancy.scope({ScopeOrder: "msk", ScopeSalesSummary: Tenancy.any_of(1, 2)}):
        assert (await ScopeOrder.objects.create(id=13)).store == "msk"
        with pytest.raises(QueryError, match="has no single value to give it"):
            await ScopeSalesSummary.objects.create(id=10)
        with pytest.raises(QueryError, match="does not match the active tenant scope"):
            await ScopeSalesSummary.objects.create(id=10, store_id=3)
        assert (await ScopeSalesSummary.objects.create(id=10, store_id=2)).store_id == 2
        # A model the scope doesn't name is written as with no scope: its tenant is trusted.
        assert (await ScopeFaq.objects.create(id=10, group="any", question="trusted")).group == "any"
        with pytest.raises(QueryError, match="no tenant is active and no explicit value was given"):
            await ScopeFaq.objects.create(id=11, question="no tenant")
    assert (await get_all_order_rows())[13] == ("msk", 0)


@pytest.mark.asyncio
async def test_save_stays_in_scope(stores):
    with Tenancy.scope(TWO_STORES):
        with pytest.raises(QueryError, match="has no single value to give it"):
            await ScopeOrder(id=20).save()
        with pytest.raises(QueryError, match="tenant other than the active one"):
            await ScopeOrder(id=20, store="spb").save()
        await ScopeOrder(id=20, store="msk").save()
        order = await ScopeOrder.objects.get(id=20)
        order.number = 5
        await order.save()
        order.store = "kzn"
        await order.save()
        assert (await get_all_order_rows())[20] == ("kzn", 5)
        order.store = "spb"
        with pytest.raises(QueryError, match="tenant other than the active one"):
            await order.save()
        with pytest.raises(QueryError, match="tenant other than the active one"):
            await stores["spb"].save()
    with Tenancy.scope(Tenancy.ALL):
        stored = await ScopeOrder.objects.get(id=20)
        stored.store = "spb"
        await stored.save()
    assert (await get_all_order_rows())[20] == ("spb", 5)


@pytest.mark.asyncio
async def test_a_forged_instance_writes_no_row_outside_the_scope(stores):
    spb_order = stores["spb"]
    with Tenancy.scope(TWO_STORES):
        forged = ScopeOrder(id=spb_order.id, store="msk", number=99)
        forged._saved_in_db = True
        try:
            await forged.save(update_fields=["number"])
        except Exception:  # noqa: BLE001 - what an unmatched UPDATE raises is not the point here
            pass
        try:
            await forged.delete()
        except Exception:  # noqa: BLE001
            pass
    assert (await get_all_order_rows())[spb_order.id] == ("spb", 3)
    with Tenancy.scope(TWO_STORES):
        forged = ScopeOrder(id=stores["kzn"].id, store="msk", number=99)
        forged._saved_in_db = True
        await forged.save(update_fields=["number"])
    assert (await get_all_order_rows())[stores["kzn"].id] == ("kzn", 99)


@pytest.mark.asyncio
async def test_update_reaches_and_moves_rows_inside_the_scope(stores):
    with Tenancy.scope(TWO_STORES):
        assert await ScopeOrder.objects.update(number=50) == 2
        assert await ScopeOrder.objects.filter(code="spb").update(number=60) == 0
        with pytest.raises(QueryError, match=r"Cannot set 'store' via .update\(\)"):
            await ScopeOrder.objects.update(store="spb")
        assert await ScopeOrder.objects.filter(code="msk").update(store="kzn") == 1
        with pytest.raises(QueryError, match=r"Cannot set 'store' via .update\(\)"):
            await ScopeOrder.objects.all_tenants().update(store="kzn")
    rows = await get_all_order_rows()
    assert rows == {1: ("kzn", 50), 2: ("kzn", 50), 3: ("spb", 3)}
    with Tenancy.scope("kzn"):
        with pytest.raises(QueryError, match=r"Cannot set 'store' via .update\(\)"):
            await ScopeOrder.objects.update(store="msk")
        assert await ScopeOrder.objects.update(store="kzn") == 2
    with pytest.raises(QueryError, match=r"Cannot set 'store' via .update\(\)"):
        await ScopeOrder.objects.all_tenants().update(store="msk")
    with Tenancy.scope(Tenancy.ALL):
        assert await ScopeOrder.objects.filter(id=3).update(store="ekb") == 1
    assert (await get_all_order_rows())[3] == ("ekb", 3)
    # The update is checked under the scope it runs in, like its WHERE.
    with Tenancy.scope("ekb"):
        move_to_msk = ScopeOrder.objects.filter(id=3).update(store="msk")
        sliced_move = ScopeOrder.objects.order_by("id")[:1].update(store="kzn")
    with Tenancy.scope(Tenancy.any_of("ekb", "msk")):
        assert await move_to_msk == 1
        with pytest.raises(QueryError, match=r"Cannot set 'store' via .update\(\)"):
            await sliced_move
    assert (await get_all_order_rows())[3] == ("msk", 3)


@pytest.mark.asyncio
async def test_delete_cascade_and_restore_stay_in_scope(stores):
    with Tenancy.scope(TWO_STORES):
        with pytest.raises(QueryError, match="does not match the active tenant scope"):
            await stores["spb"].delete()
        assert await ScopeOrder.objects.filter(number__gte=1).delete() == 2
        assert await get_stores() == []
        assert await ScopeOrderLine.objects.count() == 0
        assert await get_stores(ScopeOrder.objects.only_deleted()) == ["kzn", "msk"]
        assert await ScopeOrderLine.objects.only_deleted().count() == 4
    with Tenancy.scope("spb"):
        assert await ScopeOrder.objects.count() == 1
        assert await ScopeOrderLine.objects.count() == 2
    with Tenancy.scope({ScopeOrder: TWO_STORES, ScopeOrderLine: TWO_STORES}):
        deleted = await ScopeOrder.objects.only_deleted().get(id=stores["kzn"].id)
        await deleted.restore(cascade=True)
        assert await get_stores() == ["kzn"]
        assert await ScopeOrderLine.objects.count() == 2
        with pytest.raises(QueryError, match="does not match the active tenant scope"):
            await stores["spb"].restore()
    with Tenancy.scope(Tenancy.ALL):
        await stores["spb"].delete()
        assert await get_stores() == ["kzn"]
        await (await ScopeOrder.objects.only_deleted().get(id=stores["msk"].id)).restore(cascade=True)
        assert await get_stores() == ["kzn", "msk"]
        assert await ScopeOrderLine.objects.count() == 4


@pytest.mark.asyncio
async def test_bulk_create_fills_and_checks_the_tenant(stores):
    with Tenancy.scope("msk"):
        await ScopeOrder.objects.bulk_create([ScopeOrder(id=30), ScopeOrder(id=31, store="msk")])
    with Tenancy.scope(TWO_STORES):
        with pytest.raises(QueryError, match="has no single value to give it"):
            await ScopeOrder.objects.bulk_create([ScopeOrder(id=32)])
        with pytest.raises(QueryError, match="tenant other than the active one"):
            await ScopeOrder.objects.bulk_create([ScopeOrder(id=32, store="kzn"), ScopeOrder(id=33, store="spb")])
        await ScopeOrder.objects.bulk_create([ScopeOrder(id=32, store="kzn"), ScopeOrder(id=33, store="msk")])
    rows = await get_all_order_rows()
    assert (rows[30], rows[31], rows[32], rows[33]) == (("msk", 0), ("msk", 0), ("kzn", 0), ("msk", 0))
    with Tenancy.scope(Tenancy.ALL):
        with pytest.raises(QueryError, match="has no single value to give it"):
            await ScopeOrder.objects.bulk_create([ScopeOrder(id=34)])
        await ScopeOrder.objects.bulk_create([ScopeOrder(id=34, store="ekb")])
    assert (await get_all_order_rows())[34] == ("ekb", 0)
    with Tenancy.scope({ScopeSalesSummary: 1}):
        with pytest.raises(QueryError, match="no tenant is active"):
            await ScopeOrder.objects.bulk_create([ScopeOrder(id=35, store="msk")])


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_a_conflict_update_reaches_only_the_rows_of_the_scope(stores):
    # The unique code conflicts with a row of each store: only the scope's rows are updated.
    conflicting = [
        ScopeOrder(id=40, store="msk", code="msk", number=71),
        ScopeOrder(id=41, store="msk", code="kzn", number=72),
        ScopeOrder(id=42, store="msk", code="spb", number=73),
    ]
    with Tenancy.scope(TWO_STORES):
        await ScopeOrder.objects.bulk_create(conflicting, on_conflict=["code"], update_fields=["number"])
    assert await get_all_order_rows() == {1: ("msk", 71), 2: ("kzn", 72), 3: ("spb", 3)}
    with Tenancy.scope("kzn"):
        await ScopeOrder.objects.bulk_create(
            [ScopeOrder(id=43, code="msk", number=1), ScopeOrder(id=44, code="kzn", number=2)],
            on_conflict=["code"],
            update_fields=["number"],
        )
    assert await get_all_order_rows() == {1: ("msk", 71), 2: ("kzn", 2), 3: ("spb", 3)}
    with Tenancy.scope(Tenancy.ALL):
        await ScopeOrder.objects.bulk_create(
            [ScopeOrder(id=45, store="msk", code="spb", number=80)], on_conflict=["code"], update_fields=["number"]
        )
    assert (await get_all_order_rows())[3] == ("spb", 80)


@pytest.mark.asyncio
async def test_bulk_update_stays_in_scope(stores):
    for order in stores.values():
        order.number += 100
    with Tenancy.scope(TWO_STORES):
        with pytest.raises(QueryError, match="tenant other than the active one"):
            await ScopeOrder.objects.bulk_update(list(stores.values()), fields=["number"])
        assert await ScopeOrder.objects.bulk_update([stores["msk"], stores["kzn"]], fields=["number"]) == 2
        forged = ScopeOrder(id=stores["spb"].id, store="kzn", number=999)
        forged._saved_in_db = True
        assert await ScopeOrder.objects.bulk_update([forged], fields=["number"]) == 0
    assert await get_all_order_rows() == {1: ("msk", 101), 2: ("kzn", 102), 3: ("spb", 3)}
    with Tenancy.scope(Tenancy.ALL):
        assert await ScopeOrder.objects.bulk_update(list(stores.values()), fields=["number"]) == 3
    assert (await get_all_order_rows())[3] == ("spb", 103)


@pytest.mark.asyncio
async def test_get_or_create_and_update_or_create(stores):
    with Tenancy.scope({ScopeOrder: TWO_STORES}):
        order, created = await ScopeOrder.objects.get_or_create(code="kzn", defaults={"store": "msk"})
        assert (order.store, created) == ("kzn", False)
        with pytest.raises(QueryError, match="has no single value to give it"):
            await ScopeOrder.objects.get_or_create(code="new")
        order, created = await ScopeOrder.objects.get_or_create(code="new", defaults={"store": "kzn", "id": 50})
        assert (order.store, created) == ("kzn", True)
        with pytest.raises(QueryError, match="does not match the active tenant scope"):
            await ScopeOrder.objects.get_or_create(code="other", defaults={"store": "spb", "id": 51})
        order, created = await ScopeOrder.objects.update_or_create(code="msk", defaults={"number": 9})
        assert (order.store, order.number, created) == ("msk", 9, False)
        order, created = await ScopeOrder.objects.update_or_create(
            code="fresh", defaults={"number": 9}, create_defaults={"number": 9, "store": "msk", "id": 52}
        )
        assert (order.store, created) == ("msk", True)
    rows = await get_all_order_rows()
    assert (rows[1], rows[50], rows[52]) == (("msk", 9), ("kzn", 0), ("msk", 9))


@pytest.mark.asyncio
async def test_a_relation_target_is_visible_in_its_own_models_scope(stores):
    north, south = 1, 3
    with Tenancy.scope({ScopeOrder: "msk", ScopeCustomer: "north"}):
        assert (await ScopeOrder.objects.create(id=60, customer_id=north)).customer_id == north
        with pytest.raises(QueryError, match="a relation can't cross tenants"):
            await ScopeOrder.objects.create(id=61, customer_id=south)
        order = await ScopeOrder.objects.get(id=60)
        order.customer_id = south
        with pytest.raises(QueryError, match="a relation can't cross tenants"):
            await order.save()
        with pytest.raises(QueryError, match="a relation can't cross tenants"):
            await ScopeOrder.objects.filter(id=60).update(customer_id=south)
        with pytest.raises(QueryError, match="a relation can't cross tenants"):
            await ScopeOrder.objects.bulk_create([ScopeOrder(id=62, customer_id=south)])
    with Tenancy.scope({ScopeOrder: "msk", ScopeCustomer: Tenancy.any_of("north", "south")}):
        assert (await ScopeOrder.objects.create(id=61, customer_id=south)).customer_id == south
    with Tenancy.scope({ScopeOrder: "msk", ScopeCustomer: Tenancy.ALL}):
        assert await ScopeOrder.objects.filter(id=60).update(customer_id=south) == 1
    with Tenancy.scope({ScopeOrder: "msk"}):
        # The scope doesn't name the customers' model - the relation is trusted, as with no scope.
        assert await ScopeOrder.objects.filter(id=60).update(customer_id=north) == 1


@pytest.mark.asyncio
async def test_many_to_many_takes_each_sides_scope(stores):
    msk_order = stores["msk"]
    with Tenancy.scope({ScopeOrder: TWO_STORES, ScopeTag: Tenancy.any_of("msk", "spb")}):
        msk_tag, spb_tag = await ScopeTag.objects.order_by("id")
        await msk_order.tags.add(msk_tag, spb_tag)
        assert sorted(await msk_order.tags.all().values_list("name", flat=True)) == ["tag-msk", "tag-spb"]
        with pytest.raises(QueryError, match="belongs to a tenant other than the active one"):
            await stores["spb"].tags.add(msk_tag)
        assert await get_stores(ScopeOrder.objects.filter(tags__name="tag-spb")) == ["msk"]
    with Tenancy.scope({ScopeOrder: "msk", ScopeTag: "msk"}):
        assert await msk_order.tags.all().values_list("name", flat=True) == ["tag-msk"]
        kzn_tag = await ScopeTag.objects.all_tenants().get(name="tag-kzn")
        with pytest.raises(QueryError, match="belongs to a tenant other than the active one"):
            await msk_order.tags.add(kzn_tag)
        orders = await ScopeOrder.objects.prefetch_related("tags")
        assert [[tag.name for tag in order.tags] for order in orders] == [["tag-msk"]]
        await msk_order.tags.remove(await ScopeTag.objects.get(name="tag-msk"))
        assert await msk_order.tags.all().count() == 0
    with Tenancy.scope({ScopeOrder: "msk", ScopeTag: Tenancy.ALL}):
        assert await msk_order.tags.all().values_list("name", flat=True) == ["tag-spb"]
        await msk_order.tags.add(await ScopeTag.objects.get(name="tag-kzn"))
        await msk_order.tags.clear()
        assert await msk_order.tags.all().count() == 0
    with Tenancy.scope({ScopeOrder: "msk"}):
        with pytest.raises(QueryError, match="doesn't name ScopeTag"):
            await msk_order.tags.all()


@pytest.mark.asyncio
async def test_a_through_model_takes_its_own_scope(stores):
    msk_order = stores["msk"]
    with Tenancy.scope({ScopeOrder: "msk", ScopeLabel: Tenancy.ALL, ScopeOrderLabel: "msk"}):
        first, second, third = await ScopeLabel.objects.order_by("id")
        await msk_order.labels.add(first)
        with pytest.raises(QueryError, match="differs from the active tenant"):
            await msk_order.labels.add(second, through_defaults={"store": "kzn"})
    with Tenancy.scope({ScopeOrder: "msk", ScopeLabel: Tenancy.ALL, ScopeOrderLabel: TWO_STORES}):
        with pytest.raises(QueryError, match="name it in through_defaults"):
            await msk_order.labels.add(second)
        await msk_order.labels.add(second, through_defaults={"store": "kzn"})
        with pytest.raises(QueryError, match="differs from the active tenant"):
            await msk_order.labels.add(third, through_defaults={"store": "spb"})
        assert sorted(await msk_order.labels.all().values_list("name", flat=True)) == ["label-1", "label-2"]
    with Tenancy.scope({ScopeOrder: "msk", ScopeLabel: Tenancy.ALL, ScopeOrderLabel: "msk"}):
        assert await msk_order.labels.all().values_list("name", flat=True) == ["label-1"]
        assert await get_stores(ScopeOrder.objects.filter(labels__name="label-2")) == []
    with Tenancy.scope({ScopeOrderLabel: Tenancy.ALL}):
        assert sorted(await ScopeOrderLabel.objects.values_list("store", flat=True)) == ["kzn", "msk"]


@pytest.mark.asyncio
async def test_a_foreign_key_tenant_field(tenant_scope_context):
    shops = [await ScopeShop.objects.create(id=position, name=f"shop-{position}") for position in (1, 2, 3)]
    for shop in shops:
        with Tenancy.scope(shop.id):
            await ScopeShopOrder.objects.create(id=shop.id, title=f"order-{shop.id}")
    try:
        with Tenancy.scope(Tenancy.any_of(1, 2)):
            assert sorted(await ScopeShopOrder.objects.values_list("shop_id", flat=True)) == [1, 2]
            assert await ScopeShopOrder.objects.filter(shop__name="shop-3").count() == 0
            with pytest.raises(QueryError, match="has no single value to give it"):
                await ScopeShopOrder.objects.create(id=10, title="unnamed")
            assert (await ScopeShopOrder.objects.create(id=10, title="named", shop=shops[1])).shop_id == 2
            assert (await ScopeShopOrder.objects.create(id=11, title="by id", shop_id=1)).shop_id == 1
            with pytest.raises(QueryError, match="does not match the active tenant scope"):
                await ScopeShopOrder.objects.create(id=12, title="other", shop=shops[2])
            assert await ScopeShopOrder.objects.filter(id=10).update(shop=shops[0]) == 1
            with pytest.raises(QueryError, match=r"Cannot set 'shop_id' via .update\(\)"):
                await ScopeShopOrder.objects.filter(id=10).update(shop=shops[2])
            assert await ScopeShopOrder.objects.delete() == 4
        with Tenancy.scope({ScopeShopOrder: Tenancy.ALL}):
            assert await ScopeShopOrder.objects.values_list("shop_id", flat=True) == [3]
    finally:
        await truncate_all_models()


@pytest.mark.asyncio
async def test_a_composite_primary_key_and_relations_to_it(stores):
    with Tenancy.scope({ScopePair: TWO_STORES, ScopePairNote: "a"}):
        assert sorted(await ScopePair.objects.values_list("store", flat=True)) == ["kzn", "msk"]
        pair = await ScopePair.objects.get(pk=("kzn", 1))
        pair.title = "renamed"
        await pair.save()
        with pytest.raises(DoesNotExist):
            await ScopePair.objects.get(pk=("spb", 1))
        note = await ScopePairNote.objects.create(id=1, pair=pair, text="in scope")
        assert note.group == "a"
        spb_pair = await ScopePair.objects.all_tenants().get(pk=("spb", 1))
        with pytest.raises(QueryError, match="a relation can't cross tenants"):
            await ScopePairNote.objects.create(id=2, pair=spb_pair)
        assert await ScopePairNote.objects.filter(pair__title="renamed").count() == 1
        assert [len(found.notes) for found in await ScopePair.objects.order_by("store").prefetch_related("notes")] == [
            1,
            0,
        ]
        with pytest.raises(QueryError, match="has no single value to give it"):
            await ScopePair.objects.create(number=2)
        assert (await ScopePair.objects.create(store="msk", number=2)).pk == ("msk", 2)
        assert await ScopePair.objects.filter(number=2).delete() == 1
    with Tenancy.scope({ScopePair: "spb", ScopePairNote: "a"}):
        assert await ScopePairNote.objects.filter(pair__title="renamed").count() == 0
        assert await ScopePairNote.objects.count() == 1
        with pytest.raises(QueryError, match="does not match the active tenant scope"):
            await pair.delete()
    with Tenancy.scope({ScopePair: "kzn"}):
        await pair.delete()
    with Tenancy.scope({ScopePairNote: Tenancy.ALL}):
        assert await ScopePairNote.objects.count() == 0


@pytest.mark.asyncio
async def test_models_of_an_abstract_base_share_its_entry(tenant_scope_context):
    try:
        with Tenancy.scope("msk"):
            await ScopeInvoice.objects.create(title="invoice-msk")
            await ScopeReceipt.objects.create(title="receipt-msk")
        with Tenancy.scope("kzn"):
            await ScopeInvoice.objects.create(title="invoice-kzn")
            await ScopeReceipt.objects.create(title="receipt-kzn")
        document = ScopeInvoice.__mro__[1]
        with Tenancy.scope({document: "kzn"}):
            assert await ScopeInvoice.objects.values_list("title", flat=True) == ["invoice-kzn"]
            assert await ScopeReceipt.objects.values_list("title", flat=True) == ["receipt-kzn"]
        with Tenancy.scope({document: "kzn", ScopeReceipt: Tenancy.ALL}):
            assert await ScopeInvoice.objects.count() == 1
            assert await ScopeReceipt.objects.count() == 2
    finally:
        await truncate_all_models()


@pytest.mark.asyncio
async def test_nested_scopes_replace_the_outer_one(stores):
    with Tenancy.scope(TWO_STORES):
        assert await get_stores() == ["kzn", "msk"]
        with Tenancy.scope("spb"):
            assert await get_stores() == ["spb"]
            with Tenancy.scope({ScopeSalesSummary: 1}):
                with pytest.raises(QueryError, match="doesn't name ScopeOrder"):
                    await get_stores()
            assert await get_stores() == ["spb"]
        assert await get_stores() == ["kzn", "msk"]


@pytest.mark.asyncio
async def test_each_task_has_its_own_scope(stores):
    async def read(scope: Any) -> list[str]:
        with Tenancy.scope(scope):
            await asyncio.sleep(0)
            found = await get_stores()
            await asyncio.sleep(0)
            assert await get_stores() == found
            return found

    results = await asyncio.gather(
        read("msk"), read(TWO_STORES), read(Tenancy.ALL), read({ScopeOrder: "spb"}), read(Tenancy.any_of("kzn", "spb"))
    )
    assert results == [["msk"], ["kzn", "msk"], ["kzn", "msk", "spb"], ["spb"], ["kzn", "spb"]]
    assert Tenancy.current.get() is None


@pytest.mark.asyncio
async def test_a_queryset_runs_under_the_scope_it_is_awaited_in(stores):
    queryset = ScopeOrder.objects.order_by("id")
    with Tenancy.scope(TWO_STORES):
        assert await get_stores(queryset) == ["kzn", "msk"]
    with Tenancy.scope({ScopeOrder: "spb"}):
        assert await get_stores(queryset) == ["spb"]
    with Tenancy.scope("msk"):
        assert await get_stores(queryset) == ["msk"]
    with Tenancy.scope(TWO_STORES):
        assert await get_stores(queryset) == ["kzn", "msk"]


@pytest.mark.asyncio
async def test_a_router_sees_the_scope_of_the_query(stores):
    seen: list[tuple[Any, Any]] = []

    class RecordingRouter:
        def db_for_read(self, model: Any) -> str:
            seen.append((Tenancy.current.get(), Tenancy.get_scope(model)))
            return "models"

        def db_for_write(self, model: Any) -> str:
            return "models"

    router = HareContext.require_current().router
    router.init_routers([RecordingRouter])
    try:
        with Tenancy.scope("msk"):
            await ScopeOrder.objects.count()
        with Tenancy.scope(TWO_STORES):
            await ScopeOrder.objects.count()
        by_model = {ScopeOrder: TWO_STORES, ScopeSalesSummary: 2}
        with Tenancy.scope(by_model):
            await ScopeSalesSummary.objects.count()
            queryset = ScopeOrder.objects.all()
            rows_query = queryset.__await__
        with Tenancy.scope("spb"):
            assert rows_query is not None
            await ScopeOrder.objects.count()
    finally:
        router.init_routers([])
    assert seen[0] == ("msk", "msk")
    assert seen[1] == (TWO_STORES, TWO_STORES)
    assert seen[2][1] == 2 and seen[2][0].get_for_model(ScopeOrder) is TWO_STORES
    assert seen[3] == ("spb", "spb")


@pytest.mark.asyncio
async def test_a_pydantic_input_names_the_tenant_under_several_values(stores):
    summary_input = pydantic_model_creator(ScopeSalesSummary, name="ScopeSalesSummaryInput", exclude_readonly=True)
    assert not summary_input.model_fields["store_id"].is_required()
    with Tenancy.scope({ScopeSalesSummary: 1}):
        data = summary_input(total=5).model_dump(exclude_unset=True)
        assert (await ScopeSalesSummary.objects.create(id=70, **data)).store_id == 1
    with Tenancy.scope({ScopeSalesSummary: Tenancy.any_of(1, 2)}):
        with pytest.raises(QueryError, match="has no single value to give it"):
            await ScopeSalesSummary.objects.create(id=71, **summary_input(total=5).model_dump(exclude_unset=True))
        data = summary_input(total=5, store_id=2).model_dump(exclude_unset=True)
        assert (await ScopeSalesSummary.objects.create(id=71, **data)).store_id == 2
        with pytest.raises(QueryError, match="does not match the active tenant scope"):
            await ScopeSalesSummary.objects.create(
                id=72, **summary_input(total=5, store_id=3).model_dump(exclude_unset=True)
            )
