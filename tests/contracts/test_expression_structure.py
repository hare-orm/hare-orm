"""Expression composition preserves evaluation order and aggregate argument restrictions."""

from types import ModuleType

import pytest

from hare import Hare, Model, fields
from hare.exceptions import UnSupportedError
from hare.query.expressions import F, Window
from hare.query.functions import Max, Min
from hare.query.functions.window import FirstValue, Max as WindowMax, Min as WindowMin
from tests.contracts.constants import CONTRACT_APP, CONTRACT_CONNECTION
from tests.utils.fake_client import FakeClient


@pytest.mark.asyncio
@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
@pytest.mark.parametrize(
    ("field_type", "values", "expression", "expected"),
    [
        (fields.BigIntField, (2**63 - 1, 1, 1), F("a") + (F("b") - F("c")), 2**63 - 1),
        (fields.FloatField, (1e16, -1e16, 1.0), F("a") + (F("b") + F("c")), 0.0),
        (fields.FloatField, (1e200, 1e200, 1e-200), F("a") * (F("b") * F("c")), 1e200),
    ],
    ids=["integer-intermediate-overflow", "float-addition", "float-multiplication"],
)
async def test_nested_arithmetic_retains_its_evaluation_tree(
    contract_database, composite, field_type, values, expression, expected
):
    class NumberRecord(Model):
        number = fields.IntField(primary_key=not composite, generated=False)
        region = fields.IntField(default=7)
        if composite:
            pk = fields.CompositePrimaryKey("region", "number")
        a = field_type()
        b = field_type()
        c = field_type()

    contract_database.register_live_models([NumberRecord], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    await NumberRecord.objects.create(number=1, a=values[0], b=values[1], c=values[2])
    for number in (1, 2, 1):
        result = (
            await NumberRecord.objects.filter(number=number)
            .annotate(answer=expression)
            .values_list("answer", flat=True)
        )
        assert result == ([expected] if number == 1 else [])


@pytest.mark.database_independent
@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
@pytest.mark.parametrize(("aggregate", "window_function"), [(Min, WindowMin), (Max, WindowMax)])
def test_window_extrema_apply_the_aggregates_orderability_checks(composite, aggregate, window_function):
    class WindowRecord(Model):
        number = fields.IntField(primary_key=not composite, generated=False)
        region = fields.IntField(default=7)
        if composite:
            pk = fields.CompositePrimaryKey("region", "number")
        flag = fields.BooleanField()
        key = fields.UUIDField()
        document = fields.JSONField()

    module = ModuleType("window_extrema_contract")
    module.WindowRecord = WindowRecord
    apps = Hare.bind_models(
        {"connections": {"default": "sqlite+aiosqlite://:memory:"}, "apps": {"library": {"models": [module]}}}
    )
    query = apps.get_queryset(WindowRecord).using(FakeClient("postgresql"))
    for name in ("flag", "key", "document"):
        for expression in (aggregate(name), Window(aggregate(name)), Window(window_function(name))):
            with pytest.raises(UnSupportedError, match="database has no"):
                query.annotate(answer=expression).values_list("answer", flat=True).sql()
        # Reading a value in a window does not require the database's MIN/MAX aggregate.
        assert "FIRST_VALUE" in query.annotate(answer=Window(FirstValue(name))).sql()
    assert "OVER" in query.annotate(answer=Window(aggregate("number"))).sql()
