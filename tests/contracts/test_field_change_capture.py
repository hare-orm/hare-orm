"""Field contract matrix, the change capture column: a row of every field sample, captured into an outbox, is
inserted, updated and deleted - each event is written, its payload is strict JSON (no NaN token) and holds the
value's JSON form; a field the capture leaves out (sensitive) isn't in it."""

import enum
import json
from decimal import Decimal

import pytest

from hare import Model, fields
from hare.contrib.outbox import ChangeCapture
from hare.contrib.test import requires_features
from hare.core.hare_context import HareContext
from tests.contracts.constants import CONTRACT_APP, CONTRACT_CONNECTION
from tests.contracts.contract_models import ContractModels
from tests.contracts.field_samples import FIELD_SAMPLES
from tests.contracts.sample_database import SampleDatabase
from tests.utils.database_under_test import DatabaseUnderTest


def refuse_constant(name: str) -> None:
    """json.loads()'s parse_constant - a NaN or infinity token isn't JSON."""
    raise ValueError(f"{name} is no JSON value")


@pytest.mark.asyncio
@requires_features(supports_unique_constraints=True)
@pytest.mark.parametrize("composite", [False, True], ids=["scalar-key", "composite-key"])
async def test_captured_enum_members_keep_their_recursive_json_form(contract_database, composite):
    choices = enum.Enum(
        "CapturedChoice",
        {
            "NAN": float("nan"),
            "INFINITY": float("inf"),
            "NEGATIVE_INFINITY": float("-inf"),
            "DECIMAL": Decimal("1.50"),
        },
    )
    outbox = ContractModels.make_outbox()

    class CapturedChoiceRecord(Model):
        number = fields.IntField(primary_key=not composite, generated=False)
        if composite:
            region = fields.IntField(default=7)
            pk = fields.CompositePrimaryKey("region", "number")
        value = fields.CharEnumField(choices)

        class Meta:
            change_capture = ChangeCapture(outbox)

    contract_database.register_live_models([outbox, CapturedChoiceRecord], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    for number, member in enumerate(choices, 1):
        row = await CapturedChoiceRecord.objects.create(number=number, value=member)
        assert (await CapturedChoiceRecord.objects.get(pk=row.pk)).value is member
    payloads = await outbox.objects.order_by("sequence").values_list("payload", flat=True)
    assert [payload["after"]["value"] for payload in payloads] == ["NaN", "Infinity", "-Infinity", "1.50"]
    for payload in payloads:
        json.dumps(payload, allow_nan=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sample",
    [sample.get_parameter() for sample in FIELD_SAMPLES if sample.values and SampleDatabase.is_capturable(sample)],
)
async def test_a_captured_change_holds_the_json_form_of_the_value(contract_database, sample):
    connection = contract_database.get_connection()
    SampleDatabase.skip_unless_stored(sample)
    dialect = DatabaseUnderTest.get_dialect()
    if not dialect.features.supports_unique_constraints:
        pytest.skip(f"{dialect.name} keeps nothing unique - an outbox on it is refused when bound")
    outbox = ContractModels.make_outbox()
    models = await SampleDatabase.create(contract_database, sample, outbox)
    row_model = models[-1]
    numbers = range(1, len(sample.values) + 1)
    for number, value in zip(numbers, sample.values, strict=True):
        await row_model.objects.create(id=number, **{sample.column_name: value})
    for number, value in zip(numbers, sample.values, strict=True):
        await row_model.objects.filter(id=number).update(**{sample.column_name: value})
    await row_model.objects.all().delete()

    literals = connection.dialect.literals
    rows = await connection.execute_dicts(
        f"SELECT {literals.quote_identifier('payload')} AS payload FROM "
        f"{literals.quote_identifier(HareContext.require_current().apps.get_binding(outbox).db_table)} ORDER BY "
        f"{literals.quote_identifier('sequence')}"
    )
    payloads = [
        json.loads(row["payload"], parse_constant=refuse_constant)
        if isinstance(row["payload"], str)
        else row["payload"]
        for row in rows
    ]
    held_by_operation = {"inserted": [], "updated": [], "deleted": []}
    for payload in payloads:
        values = payload["before"] if payload["operation"] == "deleted" else payload["after"]
        held_by_operation[payload["operation"]].append(values)
    expected = list(sample.json_values) if sample.captured else []
    for operation, held in held_by_operation.items():
        assert len(held) == len(sample.values), operation
        if sample.captured:
            assert [values[sample.column_name] for values in held] == expected, operation
        else:
            assert all(sample.column_name not in values for values in held), operation
