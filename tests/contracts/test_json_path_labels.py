"""JSON path parsing preserves object labels and distinguishes missing nested values."""

import pytest
import pytest_asyncio

from hare import Model, fields
from hare.contrib.test import requires_features
from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.exceptions import IntegrityError
from hare.query.expressions import F, Q
from tests.contracts.constants import CONTRACT_APP, CONTRACT_CONNECTION


@pytest_asyncio.fixture(params=[False, True], ids=["scalar", "composite"])
async def json_path_model(contract_database, request):
    composite = request.param

    class Entry(Model):
        number = fields.IntField(primary_key=not composite, generated=False)
        region = fields.IntField(default=7)
        data = fields.JSONField(null=True)
        if composite:
            pk = fields.CompositePrimaryKey("region", "number")

    contract_database.register_live_models([Entry], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    return Entry


@pytest.mark.asyncio
async def test_numeric_looking_object_labels_keep_their_exact_spelling(json_path_model):
    model = json_path_model
    supports_typed_filter = model.objects.get_connection().dialect.filter_operators.supports_lookup(
        model.objects._binding.get_lookup_info("data__filter")
    )
    await model.objects.create(
        number=1, data={"01": 101, "001": 102, "-0": 103, "1": 999, "0": 998, "items": [10, 20]}
    )
    for _ in range(2):
        for label, value in (("01", 101), ("001", 102), ("-0", 103)):
            if supports_typed_filter:
                assert await model.objects.filter(data__filter={label: value}).values_list("number", flat=True) == [1]
            assert await model.objects.filter(**{f"data__{label}": value}).values_list("number", flat=True) == [1]
            assert await model.objects.annotate(selected=F(f"data__{label}")).values_list("selected", flat=True) == [
                value
            ]
        assert await model.objects.annotate(selected=F("data__items__0")).values_list("selected", flat=True) == [10]
        assert await model.objects.filter(**{"data__items__-1": 20}).values_list("number", flat=True) == [1]


@pytest.mark.asyncio
async def test_nested_empty_key_list_does_not_match_a_missing_path(json_path_model):
    model = json_path_model
    if not model.objects.get_connection().dialect.filter_operators.supports_lookup(
        model.objects._binding.get_lookup_info("data__filter")
    ):
        pytest.skip("The dialect has no typed JSON path filter")
    for number, document in enumerate(
        ({"nested": {}}, {"nested": None}, {"nested": []}, {"nested": 1}, {}, 17, [], None), 1
    ):
        await model.objects.create(number=number, data=document)
    for _ in range(2):
        assert await model.objects.filter(data__filter={"nested__has_keys": []}).order_by("number").values_list(
            "number", flat=True
        ) == [1, 2, 3, 4]
        assert (
            await model.objects.filter(data__filter={"nested__has_any_keys": []}).values_list("number", flat=True)
            == []
        )
        assert await model.objects.filter(data__has_keys=[]).order_by("number").values_list(
            "number", flat=True
        ) == list(range(1, 8))


@pytest.mark.asyncio
async def test_numeric_labels_keep_object_spelling_and_still_index_arrays(json_path_model):
    model = json_path_model
    await model.objects.create(number=1, data={"node": {"01": 101, "001": 102, "-0": 103, "1": 900, "0": 901}})
    await model.objects.create(number=2, data={"node": [10, 20]})
    for _ in range(2):
        for label, object_value, array_value in (("01", 101, 20), ("001", 102, 20), ("-0", 103, 10)):
            source = model.objects.annotate(selected=F(f"data__node__{label}"))
            assert await source.order_by("number").values_list("number", "selected") == [
                (1, object_value),
                (2, array_value),
            ]
            assert await model.objects.filter(**{f"data__node__{label}": array_value}).values_list(
                "number", flat=True
            ) == [2]


@pytest.mark.asyncio
@requires_features(connection_alias=CONTRACT_CONNECTION, dialect="sqlite")
@pytest.mark.parametrize("label", ["plain", 'quote"key', 'dot.quote"key', "back\\slash", "", "*", "it's", "ключ"])
async def test_sqlite_paths_and_key_predicates_keep_punctuation_in_labels(json_path_model, label):
    model = json_path_model
    await model.objects.create(number=1, data={label: 101, "nested": {"0": {label: 102}}})
    for _ in range(2):
        assert await model.objects.annotate(selected=F(f"data__{label}")).values_list("selected", flat=True) == [101]
        assert await model.objects.annotate(selected=F(f"data__nested__0__{label}")).values_list(
            "selected", flat=True
        ) == [102]
        assert await model.objects.filter(data__filter={label: 101}).values_list("number", flat=True) == [1]
        for key, expected in ((label, [1]), (label + "missing", [])):
            assert await model.objects.filter(data__has_key=key).values_list("number", flat=True) == expected
            assert await model.objects.filter(data__has_keys=[key]).values_list("number", flat=True) == expected
            assert (
                await model.objects.filter(data__has_any_keys=["absent", key]).values_list("number", flat=True)
                == expected
            )


@pytest.mark.asyncio
@requires_features(connection_alias=CONTRACT_CONNECTION, dialect="sqlite")
async def test_sqlite_key_presence_remains_native_in_a_check_constraint(contract_database):
    class RequiredLabels(Model):
        data = fields.JSONField()

        class Meta:
            constraints = (CheckConstraint(Q(data__has_keys=['dot.quote"key', "plain"]), name="required_labels"),)

    contract_database.register_live_models([RequiredLabels], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    obj = await RequiredLabels.objects.create(data={'dot.quote"key': None, "plain": True})
    assert obj.data == {'dot.quote"key': None, "plain": True}
    with pytest.raises(IntegrityError):
        await RequiredLabels.objects.create(data={"plain": True})
