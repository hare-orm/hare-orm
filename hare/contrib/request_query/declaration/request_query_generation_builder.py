from __future__ import annotations

from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING, Any
from weakref import ref

from pydantic import create_model

from hare.contrib.request_query.declaration.declaration_builder import DeclarationBuilder
from hare.contrib.request_query.declaration.filter_parameter_builder import FilterParameterBuilder
from hare.contrib.request_query.declaration.request_query_generation import RequestQueryGeneration
from hare.contrib.request_query.declaration.request_query_parameters import RequestQueryParameters
from hare.contrib.request_query.declaration.request_query_registry import RequestQueryRegistry

if TYPE_CHECKING:
    from hare.contrib.request_query.request_query import RequestQuery


class RequestQueryGenerationBuilder:
    """Build a private Pydantic subclass before publishing any of its artifacts.

    The registry lock serializes preparation and invalidation. Reentrant registry
    changes during dialect loading advance the revision and discard the candidate.
    A failure leaves both the source class and previously returned schemas intact.
    """

    @staticmethod
    def build(query_class: type[RequestQuery[Any]]) -> RequestQueryGeneration:
        queryset = query_class.get_meta_option("queryset")
        if queryset is not None:
            RequestQueryRegistry.check_model_registered(query_class, queryset.model)
        schema = create_model(
            query_class.__name__,
            __base__=query_class,
            __module__=query_class.__module__,
            __cls_kwargs__={"parameter_schema": True},
        )
        schema.__qualname__ = query_class.__qualname__
        # Pydantic inherits annotated declarations, but option fields were added
        # after class creation. Carry those fields into the unpublished schema.
        RequestQueryParameters.add_parameter_fields(
            schema,
            {name: field for name, field in query_class.model_fields.items() if name not in schema.model_fields},
        )
        options = query_class.get_request_options()
        declaration = None
        if queryset is not None:
            parameter_builder = FilterParameterBuilder(query_class)
            RequestQueryParameters.add_parameter_fields(schema, parameter_builder.build_fields(queryset))
            builder = DeclarationBuilder(schema)
            declaration = replace(
                builder.build(),
                on_describe=partial(RequestQueryRegistry.track_described_models, ref(query_class)),
            )
            RequestQueryRegistry.track_models(
                query_class,
                {queryset.model, *parameter_builder.described_models, *builder.described_models},
            )
            query_class.bound_models.add(queryset.model)
        schema.model_rebuild()
        generation = RequestQueryGeneration(schema, declaration, options)
        schema.parameter_generation = generation
        return generation
