"""Internal names coexist with physical columns of different case and near the identifier byte limit."""

import pytest

from hare.contrib.test import capture_queries
from hare.exceptions import UnSupportedError
from hare.query.expressions import F, Window
from hare.query.functions.window import RowNumber
from tests.contracts.constants import CONTRACT_APP, CONTRACT_CONNECTION
from tests.contracts.query_contract_models import QueryContractModels


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("letter_case", "long_prefix"),
    [("lower", "value_"), ("upper", "x" * 60), ("mixed", "\u5217" * 18)],
)
async def test_query_columns_do_not_collide(contract_database, letter_case, long_prefix):
    model = QueryContractModels.with_named_columns(letter_case, long_prefix)
    contract_database.register_live_models([model], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    parent = None
    for number in (1, 2, 3):
        parent = await model.objects.create(
            a=7,
            b=number,
            app=f"app{number}",
            model_name=f"model{number}",
            first=number * 10,
            second=number * 100,
            parent=parent,
        )
    first = model.objects.filter(b=1)
    others = model.objects.filter(b__gte=2)
    rows = await first.union(others, all=True).order_by("a", "b")
    assert [(type(row), row.pk, row.app, row.model_name) for row in rows] == [
        (model, (7, number), f"app{number}", f"model{number}") for number in (1, 2, 3)
    ]
    assert [obj.pk for obj in await model.objects.all().intersection(first)] == [(7, 1)]
    assert sorted(obj.pk for obj in await model.objects.all().difference(first)) == [(7, 2), (7, 3)]
    window = model.objects.annotate(position=Window(RowNumber(), order_by=("a", "b"))).filter(position__lte=2)
    assert await window.order_by("a", "b").values_list("a", "b") == [(7, 1), (7, 2)]
    assert [row.pk for row in await model.objects.order_by(F("first") * -1).distinct()] == [(7, 3), (7, 2), (7, 1)]
    if contract_database.get_connection().features.supports_recursive_queries:
        for depth in (0, 1, 2):
            walk = model.objects.filter(b=1).with_recursive("children", max_depth=depth)
            assert sorted(await walk.values_list("a", "b")) == [(7, number) for number in range(1, depth + 2)]
        walk = model.objects.filter(b=3).with_recursive("parent", max_depth=1)
        assert sorted(await walk.values_list("a", "b")) == [(7, 2), (7, 3)]
    write = (
        model.objects.filter(b=1).update(first=11, second=101).returning("first", "second", old=("first", "second"))
    )
    if contract_database.get_connection().features.supports_returning_old_new:
        assert await write == [{"first": 11, "second": 101, "old": {"first": 10, "second": 100}}]
    else:
        async with capture_queries() as counter:
            with pytest.raises(UnSupportedError):
                await write
        assert counter.count == 0
