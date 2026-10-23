"""Database defaults hare writes as typed literals on ClickHouse - a float by its bytes, a decimal of its
own scale, an address from its text, an enum member, a cast to a Dynamic or a Variant: the server keeps
the literal as written, and drift reported the column changed after every migrate."""

import os
import textwrap

import pytest

from tests.utils.cli_project_factory import project_factory  # noqa: F401

MODELS = """
import decimal
import enum

from hare.dialects.clickhouse.fields import DynamicField, Float32Field, VariantField


class Color(str, enum.Enum):
    RED = "red"


class Number(enum.IntEnum):
    ONE = 1


class Reading(Model):
    id = fields.IntField(primary_key=True, generated=False)
    ratio = fields.FloatField(db_default=1.5)
    small_ratio = Float32Field(db_default=2.5)
    price = fields.DecimalField(max_digits=10, decimal_places=2, db_default=decimal.Decimal("1.50"))
    address = fields.IPAddressField(db_default="::1")
    address_v4 = fields.IPv4AddressField(db_default="1.2.3.4")
    color = fields.CharEnumField(Color, db_default=Color.RED)
    number = fields.IntEnumField(Number, db_default=Number.ONE)
    anything = DynamicField(db_default=7)
    either = VariantField([fields.IntField(), fields.CharField(max_length=20)], db_default=8)
"""


@pytest.fixture(autouse=True)
def skip_without_clickhouse():
    if not os.getenv("HARE_TEST_DB", "").startswith("clickhouse"):
        pytest.skip("runs against the ClickHouse test server - make test_clickhouse")


@pytest.mark.asyncio
async def test_typed_defaults_are_in_sync_and_inspected_as_values_after_their_migration(project_factory) -> None:  # noqa: F811
    project = await project_factory()
    project.write_models(textwrap.dedent(MODELS))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    # inspectdb wrote the server's literals (SqlDefault("reinterpretAsFloat64(...)"), an enum's other side).
    inspected = await project.cli_ok("inspectdb")
    for declared_default in (
        "FloatField(db_default=1.5)",
        "Float32Field(db_default=2.5)",
        "db_default=Decimal('1.50')",
        "IPAddressField(db_default='::1')",
        "IPv4AddressField(db_default='1.2.3.4')",
        "db_default='red'",
        "db_default=1)",
        "DynamicField(db_default=7)",
        "db_default=8)",
    ):
        assert declared_default in inspected
