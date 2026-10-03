"""Names longer than Postgres's identifier limit: generated ones are shortened, explicit ones rejected."""

import os
import sys
import types
import uuid
from typing import Any

import pytest

from hare import fields
from hare.contrib.test.helpers import hare_test_context
from hare.core.context import HareContext
from hare.ddl import RawSQLTerm
from hare.ddl.constraints import CheckConstraint
from hare.ddl.indexes import Index
from hare.exceptions import ConfigurationError
from hare.migrations.drift import detect_drift_for_alias
from hare.models import Model

LIMIT = 63


def get_test_db_url() -> str:
    raw_db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:").replace("\\{", "{").replace("\\}", "}")
    return raw_db_url.format(uuid.uuid4().hex) if "{}" in raw_db_url else raw_db_url


def register_module(**models: Any) -> str:
    module_name = f"identifier_length_models_{uuid.uuid4().hex}"
    module = types.ModuleType(module_name)
    for name, model in models.items():
        setattr(module, name, model)
    sys.modules[module_name] = module
    return module_name


def make_long_named_models() -> tuple[type[Model], type[Model]]:
    class ShipmentLineItemWithAnExtremelyLongNameThatExceedsPostgresLimitAlpha(Model):
        id = fields.IntField(primary_key=True)
        label = fields.CharField(max_length=20)

    class ShipmentLineItemWithAnExtremelyLongNameThatExceedsPostgresLimitBravo(Model):
        id = fields.IntField(primary_key=True)
        items = fields.ManyToManyField(
            "models.ShipmentLineItemWithAnExtremelyLongNameThatExceedsPostgresLimitAlpha", related_name="bravos"
        )

    return (
        ShipmentLineItemWithAnExtremelyLongNameThatExceedsPostgresLimitAlpha,
        ShipmentLineItemWithAnExtremelyLongNameThatExceedsPostgresLimitBravo,
    )


@pytest.mark.asyncio
async def test_generated_long_names_are_shortened_distinctly_and_leave_no_drift():
    alpha, bravo = make_long_named_models()
    module_name = register_module(Alpha=alpha, Bravo=bravo)
    try:
        async with hare_test_context([module_name], db_url=get_test_db_url()):
            tables = [alpha._meta.db_table, bravo._meta.db_table]
            through = bravo._meta.fields_map["items"]
            names = [*tables, through.through, through.forward_key, through.backward_key]
            assert all(len(name.encode()) <= LIMIT for name in names), names
            assert len(set(tables)) == 2
            item = await alpha.objects.create(id=1, label="a")
            owner = await bravo.objects.create(id=1)
            await owner.items.add(item)
            assert await bravo.objects.filter(items__label="a").values_list("id", flat=True) == [1]
            context = HareContext.get_current()
            apps_config = {"models": {"models": [module_name], "default_connection": "default"}}
            result = await detect_drift_for_alias(context.apps, apps_config, "default")
            assert result.operations == []
            assert result.untracked_tables == []
    finally:
        del sys.modules[module_name]


def make_model_with(**attributes: Any) -> type[Model]:
    meta = attributes.pop("Meta", None)
    body: dict[str, Any] = {"id": fields.IntField(primary_key=True), "__module__": __name__, **attributes}
    if meta is not None:
        body["Meta"] = meta
    return type("Widget", (Model,), body)


LONG = "x" * (LIMIT + 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attributes", "message"),
    [
        ({"Meta": type("Meta", (), {"table": LONG})}, "the table"),
        ({"label": fields.CharField(max_length=20, source_field=LONG)}, "the column of 'label'"),
        ({LONG: fields.IntField(null=True)}, f"the column of '{LONG}'"),
        (
            {"label": fields.IntField(), "Meta": type("Meta", (), {"indexes": [Index(fields=["label"], name=LONG)]})},
            "the index",
        ),
        (
            {
                "label": fields.IntField(),
                "Meta": type("Meta", (), {"constraints": [CheckConstraint(check=RawSQLTerm("label > 0"), name=LONG)]}),
            },
            "the constraint",
        ),
    ],
)
async def test_explicit_names_over_the_limit_are_rejected(attributes, message):
    module_name = register_module(Widget=make_model_with(**attributes))
    try:
        with pytest.raises(
            ConfigurationError,
            match=f"{message} .* longer than 63 bytes, the identifier limit of a registered dialect",
        ):
            async with hare_test_context([module_name], db_url="sqlite://:memory:"):
                pass
    finally:
        del sys.modules[module_name]
