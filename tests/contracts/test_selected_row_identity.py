"""Model combinations and summaries retain the actual selected row's identity."""

import pytest
import pytest_asyncio

from hare import Model, Transactions, fields
from hare.core.hare_context import HareContext
from hare.query.expressions import Exists, F, Subquery, Value
from hare.query.functions import Abs, Sum
from hare.query.statements.select.model_rows_query import ModelRowsQuery
from hare.sql import Field as SqlField
from tests.contracts.constants import CONTRACT_APP, CONTRACT_CONNECTION


@pytest_asyncio.fixture(
    params=[fields.ForeignKeyField, fields.OneToOneField, "reverse"],
    ids=["foreign-key", "one-to-one", "reverse-one-to-one"],
)
async def keyless_related_rows(contract_database, request):
    connection = contract_database.get_connection(CONTRACT_CONNECTION)
    if not connection.features.supports_unique_constraints:
        pytest.skip("A keyless relation target requires a declared unique target field")

    class KeylessTarget(Model):
        code = fields.IntField(unique=True, source_field="source_code")
        note = fields.TextField(null=True)
        data = fields.JSONField(null=True)
        if request.param == "reverse":
            reference = fields.OneToOneField(
                f"{CONTRACT_APP}.KeylessReference", related_name="target", source_field="reference_wire"
            )

        class Meta:
            primary_key = None

    class KeylessReference(Model):
        id = fields.IntField(primary_key=True, generated=False)
        if request.param != "reverse":
            target = request.param(
                f"{CONTRACT_APP}.KeylessTarget", to_field="code", null=True, source_field="target_wire"
            )

    contract_database.register_live_models([KeylessTarget, KeylessReference], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    for number, code in ((1, 0), (2, 7)):
        if request.param == "reverse":
            reference = await KeylessReference.objects.create(id=number)
            await KeylessTarget.objects.create(code=code, reference=reference)
        else:
            target = await KeylessTarget.objects.create(code=code)
            await KeylessReference.objects.create(id=number, target=target)
    await KeylessReference.objects.create(id=3, **({} if request.param == "reverse" else {"target": None}))
    identity_field = "reference_id" if request.param == "reverse" else "code"
    return KeylessReference, KeylessTarget, identity_field


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", ["note", "data"])
@pytest.mark.parametrize("read", ["await", "iterator", "get", "stream"])
async def test_partial_keyless_join_keeps_existing_all_null_related_rows(keyless_related_rows, selected, read):
    model, target_model, identity_field = keyless_related_rows
    query = model.objects.select_related("target").only("id", f"target__{selected}").order_by("id")
    if read == "iterator":
        rows = [obj async for obj in query.iterator(chunk_size=1)]
    elif read == "get":
        rows = [await query.get(id=key) for key in (1, 2, 3)]
    elif read == "stream":
        async with Transactions.atomic():
            rows = [obj async for obj in query.stream(chunk_size=1)]
    else:
        rows = await query
    assert [obj.id for obj in rows] == [1, 2, 3]
    for obj, code in zip(rows[:2], (0, 7), strict=True):
        assert isinstance(obj.target, target_model)
        assert getattr(obj.target, selected) is None
        with pytest.raises(AttributeError):
            getattr(obj.target, "data" if selected == "note" else "note")
        assert getattr(obj.target, identity_field) == (obj.id if identity_field == "reference_id" else code)
    assert rows[2].target is None


@pytest_asyncio.fixture(params=[False, True], ids=["scalar-key", "composite-key"])
async def selected_rows(contract_database, request):
    composite = request.param

    class ProjectionParent(Model):
        region = fields.IntField(source_field="number")
        number = fields.IntField(source_field="region")
        pk = fields.CompositePrimaryKey("region", "number")
        value = fields.IntField(source_field="id")

    class ProjectionRecord(Model):
        id = fields.IntField(primary_key=not composite, generated=False, source_field="value")
        value = fields.IntField(source_field="id")
        noise = fields.IntField()
        parent = fields.ForeignKeyField(
            f"{CONTRACT_APP}.ProjectionParent", related_name="records", db_constraint=False
        )
        if composite:
            part = fields.IntField()
            pk = fields.CompositePrimaryKey("id", "part")

    contract_database.register_live_models([ProjectionParent, ProjectionRecord], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    parent = await ProjectionParent.objects.create(region=1, number=9, value=10)
    for number, value in enumerate((1, 1, 2, 2, 4, 4), 1):
        await ProjectionRecord.objects.create(
            id=number, value=value, noise=number, parent=parent, **({"part": 9} if composite else {})
        )
    return ProjectionRecord


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        (Abs(-(SqlField("id") + SqlField("noise"))), [2, 3, 5, 6, 9, 10]),
        (Abs(-(SqlField("id") - SqlField("noise"))), [0, 1, 1, 2, 1, 2]),
        (Abs(-(-SqlField("id"))), [1, 1, 2, 2, 4, 4]),
    ],
)
async def test_sql_term_unary_minus_preserves_function_arguments(selected_rows, expression, expected):
    # SQL terms address the physical column: this model stores its value in "id".
    query = selected_rows.objects.annotate(answer=expression).order_by("id").values_list("answer", flat=True)
    assert await query == expected
    assert await query == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("deferred", [False, True], ids=["only", "defer"])
@pytest.mark.parametrize("hidden_ordering", [False, True])
async def test_partial_distinct_summaries_use_the_selected_cells(selected_rows, deferred, hidden_ordering):
    model = selected_rows
    query = model.objects.all()
    if deferred:
        query = query.defer(
            *[
                name
                for name in HareContext.require_current().apps.get_binding(model).fields_db_projection
                if name != "value"
            ]
        )
    else:
        query = query.only("value")
    query = query.order_by("noise" if hidden_ordering else "value").distinct()
    expected = [1, 1, 2, 2, 4, 4] if hidden_ordering else [1, 2, 4]
    for _ in range(2):
        assert [row.value for row in await query] == expected
        assert await query.count() == len(expected)
        assert await query.aggregate(total=Sum("value")) == {"total": sum(expected)}
        assert await query[len(expected) - 1 :].exists()
        assert not await query[len(expected) :].exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["model", "values", "partial-distinct", "union"])
async def test_embedded_exists_preserves_empty_slices_on_reused_plans(selected_rows, selection):
    model = selected_rows
    if selection == "union":
        source = model.objects.filter(id__lte=3).union(model.objects.filter(id__gte=4)).order_by("id")
    elif selection == "partial-distinct":
        source = model.objects.only("value").order_by("value").distinct()
    elif selection == "values":
        source = model.objects.order_by("value").values("value").distinct()
    else:
        source = model.objects.order_by("id")
    source = source.offset(1)
    for limit in (0, 1, 0, 2):
        query = source.limit(limit)
        expected = limit != 0
        assert await query.exists() is expected
        assert (
            await model.objects.order_by("id").annotate(present=Exists(query)).values_list("present", flat=True)
            == [expected] * 6
        )
        assert await model.objects.filter(Exists(query)).count() == (6 if expected else 0)
        assert await model.objects.filter(~Exists(query)).count() == (0 if expected else 6)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["union", "intersection", "difference"])
async def test_model_set_operations_map_logical_fields_before_combining(selected_rows, operation):
    model = selected_rows
    left = model.objects.filter(id__lte=3).only("pk", "value")
    right = model.objects.filter(id__gte=3).only("value", "pk")
    expected_ids = {"union": [1, 2, 3, 4, 5, 6], "intersection": [3], "difference": [1, 2]}[operation]
    expected_values = dict(enumerate((1, 1, 2, 2, 4, 4), 1))
    query = getattr(left, operation)(right).order_by("id")
    for _ in range(2):
        assert [(row.id, row.value) for row in await query] == [
            (number, expected_values[number]) for number in expected_ids
        ]
        assert await query.count() == len(expected_ids)
        assert await model.objects.filter(pk__in=query).order_by("id").values_list("id", flat=True) == expected_ids
        assert await query.aggregate(total=Sum("value")) == {
            "total": sum(expected_values[number] for number in expected_ids)
        }


@pytest.mark.asyncio
async def test_model_union_trims_hidden_columns_after_branch_distinct(selected_rows):
    source = selected_rows.objects.only("value").order_by("noise").distinct()
    empty = selected_rows.objects.none().only("value")
    assert [row.value for row in await source.union(empty, all=True).order_by("value")] == [1, 1, 2, 2, 4, 4]
    assert [row.value for row in await source.union(empty).order_by("value")] == [1, 2, 4]
    assert await source.union(empty, all=True).aggregate(total=Sum("value")) == {"total": 14}


@pytest.mark.asyncio
async def test_values_summary_uses_sql_positions_without_changing_public_output_order(selected_rows):
    query = selected_rows.objects.annotate(marker=Value(10)).order_by("value").values("marker", "value")
    assert await query == [{"marker": 10, "value": value} for value in (1, 1, 2, 2, 4, 4)]
    assert await query[:3].aggregate(total=Sum("value"), markers=Sum("marker")) == {
        "total": 4,
        "markers": 30,
    }
    assert await query.union(query).aggregate(total=Sum("value")) == {"total": 7}


@pytest.mark.asyncio
async def test_positional_subqueries_keep_requested_annotation_and_field_order(selected_rows):
    parent_model = HareContext.require_current().apps.get_binding(selected_rows).fields_map["parent"].related_model
    expected = [(1, 1), (2, 1), (3, 2), (4, 2), (5, 4), (6, 4)]
    candidates = set(expected) | {(number, region) for region, number in expected}
    for region, number in candidates:
        await parent_model.objects.create(region=region, number=number, value=0)
    for _ in range(2):
        inner = selected_rows.objects.annotate(annotation=F("id")).order_by("id").values_list("annotation", "value")
        assert await inner == expected
        for argument in (inner, Subquery(inner)):
            assert (
                await parent_model.objects.filter(pk__in=argument)
                .order_by("region", "number")
                .values_list("pk", flat=True)
                == expected
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["full", "only", "defer", "early-related"])
async def test_related_cells_cannot_take_a_public_annotation_label(selected_rows, selection):
    query = selected_rows.objects.using(CONTRACT_CONNECTION)
    if selection == "only":
        query = query.only("value")
    elif selection == "defer":
        query = query.defer("noise")
    elif selection == "early-related":
        query = query.only("value", "parent__pk", "parent__value")
    query = query.select_related("parent").order_by("id")
    built = ModelRowsQuery(query)._get_scoped_copy()
    built._make_subquery()
    label = next(
        term.alias
        for term in built.query._selects
        if getattr(term, "name", None) == "id" and "." in (term.alias or "")
    )
    for marker in (77, 88, 77):
        annotated = query.annotate(**{label: Value(marker)})
        for iterator in (False, True):
            rows = [row async for row in annotated.iterator(chunk_size=2)] if iterator else await annotated
            assert [row.value for row in rows] == [1, 1, 2, 2, 4, 4]
            assert [row.parent.value for row in rows] == [10] * 6
            assert [getattr(row, label) for row in rows] == [marker] * 6
