"""Every registration drops the caches built from the registries: a renderer, type mapping, lookup,
path transform, QuerySet method, dialect or driver added after queries have run takes effect on
the next query - no cached SQL, row layout or filter description outlives it."""

import ast
from pathlib import Path

import pytest

from hare.contrib.test import capture_queries
from hare.core.registries import Registries
from hare.exceptions import FieldError
from hare.fields import CharField, Field
from hare.query.filters import FieldLookup
from hare.query.functions import Upper
from hare.query.plans.statement.statement_plans import StatementPlans
from hare.query.rows.native.hydration_layout import HydrationLayout
from hare.sql import functions
from hare.sql.terms import Term
from tests.testmodels import Tournament

#: Methods named ``register*`` that are not a registry of what hare builds SQL or reads rows from,
#: or that only pass their entries on to one that is.
NOT_REGISTRIES = {
    ("hare/cli/plugins/cli_command_registry.py", "register"): "a CLI command",
    ("hare/core/hare_context.py", "register_live_models"): "binds models, which build their own caches",
    ("hare/core/hare.py", "register_live_models"): "passes the models to HareContext.register_live_models()",
    ("hare/migrations/state/state_apps.py", "register_model"): "a migration state's model",
    ("hare/core/caching/caches.py", "register"): "a cache, dropped by the registries' changes",
    ("hare/dialects/sqlite/parameters/sqlite_parameter_adapters.py", "register"): "sqlite3's own parameter adapters",
    (
        "hare/dialects/sqlite/renderers/sqlite_defaults.py",
        "register",
    ): "passes its renderers to TermRenderers.register()",
    ("hare/dialects/sqlite/search/sqlite_search_renderers.py", "register"): (
        "passes its renderers to TermRenderers.register()"
    ),
    ("hare/dialects/sqlite/vectors/sqlite_vector_renderers.py", "register"): (
        "passes its renderers to TermRenderers.register()"
    ),
    ("hare/dialects/sqlite/spatial/sqlite_spatial_renderers.py", "register"): (
        "passes its renderers to TermRenderers.register()"
    ),
    (
        "hare/dialects/postgresql/renderers/postgresql_defaults.py",
        "register",
    ): "passes its renderers to TermRenderers.register()",
    ("hare/dialects/postgresql/spatial/postgresql_spatial_renderers.py", "register"): (
        "passes its renderers to TermRenderers.register()"
    ),
    ("hare/dialects/sqlite/renderers/sqlite_json_renderers.py", "register"): (
        "passes its renderers to TermRenderers.register()"
    ),
    ("hare/dialects/sqlite/renderers/sqlite_number_renderers.py", "register"): (
        "passes its renderers to TermRenderers.register()"
    ),
    ("hare/dialects/sqlite/renderers/sqlite_temporal_renderers.py", "register"): (
        "passes its renderers to TermRenderers.register()"
    ),
    ("hare/dialects/sqlite/renderers/sqlite_text_renderers.py", "register"): (
        "passes its renderers to TermRenderers.register()"
    ),
    ("hare/dialects/postgresql/renderers/postgresql_json_renderers.py", "register"): (
        "passes its renderers to TermRenderers.register()"
    ),
    ("hare/dialects/postgresql/renderers/postgresql_number_renderers.py", "register"): (
        "passes its renderers to TermRenderers.register()"
    ),
    ("hare/dialects/postgresql/renderers/postgresql_temporal_renderers.py", "register"): (
        "passes its renderers to TermRenderers.register()"
    ),
    ("hare/dialects/postgresql/renderers/postgresql_text_renderers.py", "register"): (
        "passes its renderers to TermRenderers.register()"
    ),
    ("hare/dialects/postgresql/vectors/postgresql_vector_renderers.py", "register"): (
        "passes its renderers to TermRenderers.register()"
    ),
    ("hare/dialects/clickhouse/renderers/clickhouse_aggregate_renderers.py", "register"): (
        "passes its renderers to TermRenderers.register_function()"
    ),
    ("hare/dialects/clickhouse/renderers/clickhouse_cast_renderers.py", "register"): (
        "passes its renderer to TermRenderers.register()"
    ),
    ("hare/dialects/clickhouse/renderers/clickhouse_condition_renderers.py", "register"): (
        "passes its renderers to TermRenderers.register()"
    ),
    ("hare/dialects/clickhouse/renderers/clickhouse_json_renderers.py", "register"): (
        "passes its renderers to TermRenderers.register()"
    ),
    ("hare/dialects/clickhouse/renderers/clickhouse_number_renderers.py", "register"): (
        "passes its renderers to TermRenderers.register()"
    ),
    ("hare/dialects/clickhouse/renderers/clickhouse_temporal_renderers.py", "register"): (
        "passes its renderers to TermRenderers.register()"
    ),
    ("hare/dialects/clickhouse/renderers/clickhouse_text_renderers.py", "register"): (
        "passes its renderers to TermRenderers.register()"
    ),
    ("hare/models/write/rollback_restores.py", "register_rollback_restore"): (
        "an instance value put back when its transaction rolls back"
    ),
    ("hare/models/write/rollback_restores.py", "register_rollback_restores"): (
        "instance values put back when their transaction rolls back"
    ),
    ("hare/query/statements/select/values/values_output.py", "register_selected_annotations"): (
        "the selected annotations of one values() query"
    ),
    (
        "hare/dialects/postgresql/lookups/unaccent_transform.py",
        "register",
    ): "passes its transform to Field.register_transform()",
    (
        "hare/dialects/postgresql/lookups/trigram/postgresql_trigram_lookups.py",
        "register",
    ): "passes its lookups to Field.register_lookup()",
    ("hare/dialects/clickhouse/lookups/clickhouse_global_lookups.py", "register"): (
        "passes its lookups to Field.register_lookup()"
    ),
    ("hare/dialects/clickhouse/query/clickhouse_queryset_methods.py", "register"): (
        "passes its methods to QuerySetExtensions.register()"
    ),
    ("hare/dialects/clickhouse/renderers/clickhouse_container_renderers.py", "register"): (
        "passes its renderers to TermRenderers.register()"
    ),
    ("hare/dialects/clickhouse/spatial/clickhouse_spatial_renderers.py", "register"): (
        "passes its renderers to TermRenderers.register()"
    ),
    ("hare/dialects/postgresql/renderers/postgresql_container_renderers.py", "register"): (
        "passes its renderers to TermRenderers.register()"
    ),
}


def calls_registries_changed(function: ast.FunctionDef) -> bool:
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "changed"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "Registries"
        for node in ast.walk(function)
    )


def test_every_registration_drops_the_caches():
    missing = []
    for path in sorted(Path("hare").rglob("*.py")):
        relative_path = path.as_posix()
        if relative_path.startswith(("hare/contrib/admin/", "hare/contrib/ui/", "hare/contrib/site/")):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.FunctionDef) or not (
                node.name == "register" or node.name.startswith("register_")
            ):
                continue
            if (relative_path, node.name) in NOT_REGISTRIES:
                continue
            if not calls_registries_changed(node):
                missing.append(f"{relative_path}:{node.lineno} {node.name}()")
    assert missing == []


def equals_lookup(field: CharField | None) -> FieldLookup:
    def operator(term: Term, value: str) -> Term:
        return term == value

    return FieldLookup(operator)


@pytest.mark.asyncio
async def test_a_lookup_registered_after_queries_ran_filters_and_is_described(db):
    await Tournament.objects.create(name="first")
    await Tournament.objects.filter(name__startswith="f").count()
    CharField.register_lookup("registry_test_equals", equals_lookup)
    try:
        assert await Tournament.objects.filter(name__registry_test_equals="first").values_list("name", flat=True) == [
            "first"
        ]
        assert (
            Tournament._meta.get_lookup_info("name__registry_test_equals").field is Tournament._meta.fields_map["name"]
        )
    finally:
        del CharField.registered_lookups["registry_test_equals"]
        Registries.changed()
    with pytest.raises(FieldError):
        Tournament._meta.get_lookup_info("name__registry_test_equals")


def every_value_equals_lookup(field: Field | None) -> FieldLookup:
    def operator(term: Term, value: str) -> Term:
        return term == value

    return FieldLookup(operator)


@pytest.mark.asyncio
async def test_a_lookup_registered_on_field_is_a_lookup_of_annotations(db):
    await Tournament.objects.create(name="first")
    shouted = Tournament.objects.annotate(shouted=Upper("name"))
    assert await shouted.filter(shouted="FIRST").count() == 1
    Field.register_lookup("registry_test_every_value", every_value_equals_lookup, value_type=str)
    try:
        shouted = Tournament.objects.annotate(shouted=Upper("name"))
        assert await shouted.filter(shouted__registry_test_every_value="FIRST").count() == 1
        assert await shouted.filter(shouted__registry_test_every_value="first").count() == 0
        assert await Tournament.objects.filter(name__registry_test_every_value="first").count() == 1
        assert await Tournament.objects.filter(id__registry_test_every_value=0).count() == 0
        described = shouted.get_lookup_info("shouted__registry_test_every_value")
        assert (described.lookup, described.value_type) == ("registry_test_every_value", str)
    finally:
        del Field.registered_lookups["registry_test_every_value"]
        Registries.changed()
    with pytest.raises(FieldError):
        await Tournament.objects.annotate(shouted=Upper("name")).filter(shouted__registry_test_every_value="FIRST")


@pytest.mark.asyncio
async def test_a_renderer_registered_after_queries_ran_changes_their_sql(db):
    await Tournament.objects.create(name="first")
    connection = Tournament.get_connection()
    renderers = connection.dialect.renderers
    queryset = Tournament.objects.annotate(shouted=Upper("name")).values_list("shouted", flat=True)
    assert await queryset == ["FIRST"]
    assert len(StatementPlans.plans) > 0
    assert HydrationLayout.layouts.get_model_bucket(Tournament)

    previous = renderers.name_renderers.get(functions.Upper)
    renderers.register_name(functions.Upper, lambda function, ctx: "LOWER")
    try:
        assert len(StatementPlans.plans) == 0
        assert not HydrationLayout.layouts.get_model_bucket(Tournament)
        async with capture_queries() as counter:
            assert await queryset == ["first"]
        assert "LOWER(" in counter.queries[-1]
    finally:
        if previous is None:
            del renderers.name_renderers[functions.Upper]
            renderers.name_renderers_by_term_class.clear()
            Registries.changed()
        else:
            renderers.register_name(functions.Upper, previous)
    assert await queryset == ["FIRST"]
