"""The cases of the contrib model matrix - every model hare ships for applications to build on, and every
mechanism of its models that adds columns, keys or tables."""

from tests.contracts.contrib_models.contrib_model_case import ContribModelCase
from tests.contracts.contrib_models.contrib_models import ContribModels

CONTRIB_MODEL_CASES = (
    ContribModelCase("outbox_event_subclass", ContribModels.make_outbox_subclass),
    ContribModelCase("versioned_model_with_soft_delete", ContribModels.make_soft_delete_versioned),
    ContribModelCase("tenant", ContribModels.make_tenant_model),
    ContribModelCase(
        "encrypted_with_blind_index", ContribModels.make_encrypted_with_blind_index, requires=("encryption",)
    ),
    ContribModelCase("gis", ContribModels.make_gis_model, needs_constraints=False),
    ContribModelCase("vector", ContribModels.make_vector_model, needs_constraints=False),
    ContribModelCase("generic_foreign_key", ContribModels.make_generic_foreign_key_models),
    ContribModelCase("composite_primary_key", ContribModels.make_composite_key_model),
    ContribModelCase("many_to_many_through", ContribModels.make_many_to_many_through_models),
)
