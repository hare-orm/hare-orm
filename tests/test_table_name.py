import sys
import types

import pytest
import pytest_asyncio

from hare import fields
from hare.core.config import HareConfig
from hare.core.context import HareContext
from hare.models import Model


def table_name_generator(model_cls: type[Model]):
    return f"test_{model_cls.__name__.lower()}"


def m2m_order_dependence_table_name_generator(model_cls: type[Model]) -> str:
    return f"tbl_{model_cls.__name__.lower()}"


async def _m2m_through_table_name_for_order(app_label_order: list[str]) -> str:
    """Builds a fresh owner/target model pair in throwaway modules (so state never leaks
    between the two orderings this is called with) and returns the through-table name hare
    computed for their M2M relation, with apps registered in `app_label_order`."""
    unique_suffix = "_".join(app_label_order)
    owner_module_name = f"tests._order_dep_owner_{unique_suffix}"
    target_module_name = f"tests._order_dep_target_{unique_suffix}"

    class OrderDepTarget(Model):
        id = fields.IntField(primary_key=True)

        owners: fields.ManyToManyRelation["OrderDepOwner"]

        class Meta:
            app = "order_dep_target"

    class OrderDepOwner(Model):
        id = fields.IntField(primary_key=True)
        targets: fields.ManyToManyRelation[OrderDepTarget] = fields.ManyToManyField(
            "order_dep_target.OrderDepTarget", db_constraint=False
        )

        class Meta:
            app = "order_dep_owner"

    owner_module = types.ModuleType(owner_module_name)
    setattr(owner_module, "OrderDepOwner", OrderDepOwner)  # noqa: B010
    target_module = types.ModuleType(target_module_name)
    setattr(target_module, "OrderDepTarget", OrderDepTarget)  # noqa: B010
    sys.modules[owner_module_name] = owner_module
    sys.modules[target_module_name] = target_module

    apps_config = {
        "order_dep_owner": {"models": [owner_module_name], "default_connection": "default"},
        "order_dep_target": {"models": [target_module_name], "default_connection": "default"},
    }
    ordered_apps_config = {label: apps_config[label] for label in app_label_order}

    ctx = HareContext()
    try:
        async with ctx:
            await ctx.init(
                config={
                    "connections": {"default": "sqlite://:memory:"},
                    "apps": ordered_apps_config,
                },
                table_name_generator=m2m_order_dependence_table_name_generator,
            )
            return OrderDepOwner._meta.fields_map["targets"].through
    finally:
        sys.modules.pop(owner_module_name, None)
        sys.modules.pop(target_module_name, None)


class Tournament(Model):
    id = fields.IntField(primary_key=True)
    name = fields.TextField()
    created_at = fields.DatetimeField(auto_now_add=True)


class CustomTable(Model):
    id = fields.IntField(primary_key=True)
    name = fields.TextField()

    class Meta:
        table = "my_custom_table"


@pytest_asyncio.fixture
async def table_name_db():
    """Fixture for table name generator tests with in-memory SQLite."""
    ctx = HareContext()
    async with ctx:
        await ctx.init(
            HareConfig.from_db_url("sqlite://:memory:", {"models": [__name__]}),
            table_name_generator=table_name_generator,
        )
        await ctx.generate_schemas()
        yield ctx


@pytest.mark.asyncio
async def test_glabal_name_generator(table_name_db):
    assert Tournament._meta.db_table == "test_tournament"


@pytest.mark.asyncio
async def test_custom_table_name_precedence(table_name_db):
    assert CustomTable._meta.db_table == "my_custom_table"


@pytest.mark.asyncio
async def test_m2m_through_table_name_independent_of_app_registration_order():
    """The M2M through-table name for a relation between two custom-named models must not
    depend on which of the two apps happens to be processed first while building relations -
    a semantically identical relation must always get the same through-table name."""
    owner_registered_first = await _m2m_through_table_name_for_order(["order_dep_owner", "order_dep_target"])
    target_registered_first = await _m2m_through_table_name_for_order(["order_dep_target", "order_dep_owner"])

    assert owner_registered_first == target_registered_first == "tbl_orderdepowner_tbl_orderdeptarget"
