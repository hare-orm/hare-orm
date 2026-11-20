"""A published plan's references carry the dialect's conversion of their field, resolved once - a query
on the plan converts its values without looking the conversion up again."""

from unittest.mock import patch

import pytest

from hare.core.caching.caches import Caches
from hare.core.hare_context import HareContext
from hare.dialects.base.types.type_registry import TypeRegistry
from hare.native.native_modules import NativeModules
from hare.query.expressions.value_references.value_reference_types import ScalarValueReference
from tests.shared_models.testmodels import Tournament

pytestmark = pytest.mark.plan_verification_after_test


@pytest.mark.asyncio
async def test_a_published_filter_value_converts_through_the_resolved_conversion(db):
    obj = await Tournament.objects.create(name="resolved")
    Caches.forget_model_caches([HareContext.require_current().apps.get_binding(Tournament)])
    bindings_class = NativeModules.rows.ParameterBindings
    references = []

    def record_bindings(binders, repeated_values):
        references.extend(
            binder.__self__
            for group in binders
            for binder in group
            if isinstance(binder.__self__, ScalarValueReference)
        )
        return bindings_class(binders, repeated_values)

    with patch.object(NativeModules.rows, "ParameterBindings", side_effect=record_bindings):
        assert (await Tournament.objects.get(name=obj.name)).pk == obj.pk
    assert references
    # The name column's own writer - the native one a CharField of built-in conversion gives.
    assert any(isinstance(reference.convert, NativeModules.rows.TypedColumnWriter) for reference in references)


@pytest.mark.asyncio
async def test_a_query_on_the_plan_looks_no_conversion_up(db):
    obj = await Tournament.objects.create(name="no lookup")
    Caches.forget_model_caches([HareContext.require_current().apps.get_binding(Tournament)])
    await Tournament.objects.get(name=obj.name)
    with (
        patch.object(TypeRegistry, "get_lookup_value", side_effect=AssertionError("looked up per query")),
        patch.object(TypeRegistry, "get_lookup_converter", side_effect=AssertionError("looked up per query")),
    ):
        assert (await Tournament.objects.get(name=obj.name)).pk == obj.pk
