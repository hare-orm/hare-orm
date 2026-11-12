from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from hare.dialects.base.parameters.sql_parameters import SqlParameters
from hare.query.expressions.value_references.value_reference_types import (
    CompositeKeyValueReference,
    ContainerValueReference,
    EncodedValueReference,
    LikeValueReference,
    ListParameterValueReference,
    ListValueReference,
    LiteralValueReference,
    OpenRangeValueReference,
    RangeValueReference,
    RelatedKeyValueReference,
    RelatedValueReference,
    RowListValueReference,
    ScalarValueReference,
    ValueReference,
    WriteValueReference,
)
from hare.query.filters.lookups.value_encoders import ValueEncoders
from hare.query.plans.statement.parameter_slot import ParameterSlot
from hare.sql.terms.array import Array
from hare.sql.terms.parameters.recording_parameterizer import RecordingParameterizer
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:
    from hare.dialects.base.types.type_registry import TypeRegistry


class ParameterPreparation:
    """Detach value conversions from a SQL tree after its single recording render.

    The identity map exists only during preparation, when the source terms are alive.
    Published references contain immutable slots and conversion settings. Neither binding
    nor validation searches an old SQL tree or reconstructs a criterion.
    """

    def __init__(self, parameterizer: RecordingParameterizer, parameters: SqlParameters, types: TypeRegistry) -> None:
        self.parameters = parameters
        #: The dialect's type registry - the conversions of the published references are resolved by it once.
        self.types = types
        self.positions: dict[int, list[int]] = {}
        self.bound_positions: set[int] = set()
        for position, source in enumerate(parameterizer.sources):
            self.positions.setdefault(id(source), []).append(position)
        self.literals = {id(source) for source in parameterizer.literal_sources}

    def slot(self, source: Term | ParameterSlot) -> ParameterSlot:
        """Describe every SQL occurrence of a source without retaining the source."""
        if isinstance(source, ParameterSlot):
            return source
        value = (
            source.value
            if isinstance(source, ValueWrapper)
            else source.original_value
            if isinstance(source, Array)
            else source
        )
        source_id = id(source)
        positions = tuple(self.positions.get(source_id, ()))
        self.bound_positions.update(positions)
        options = self.parameters.get_parameter_plan_options(value)
        return ParameterSlot(
            positions,
            type(value),
            source_id in self.literals,
            options,
            self.parameters.get_parameter_plan_options if options is not None else None,
        )

    def prepare(self, reference: object) -> ValueReference | None:
        """Bind the declared targets of a conversion; unknown conversion contracts are not cached."""
        if isinstance(reference, ScalarValueReference):
            return replace(
                reference,
                wrapper=self.slot(reference.wrapper),
                convert=self.types.get_lookup_writer(reference.field),
            )
        if isinstance(reference, (WriteValueReference, ContainerValueReference)):
            return replace(
                reference,
                wrapper=self.slot(reference.wrapper),
                convert=self.types.get_db_writer(reference.field),
            )
        if isinstance(reference, EncodedValueReference):
            return replace(
                reference,
                wrapper=self.slot(reference.wrapper),
                value_encoder=ValueEncoders.prepare(reference.value_encoder, reference.field, self.types),
            )
        if isinstance(reference, (LiteralValueReference, LikeValueReference)):
            return replace(reference, wrapper=self.slot(reference.wrapper))
        if isinstance(reference, RangeValueReference):
            return replace(
                reference,
                lower=self.slot(reference.lower),
                upper=self.slot(reference.upper),
                convert=self.types.get_lookup_writer(reference.field),
            )
        if isinstance(reference, OpenRangeValueReference):
            return replace(
                reference,
                bound=self.slot(reference.bound),
                convert=self.types.get_lookup_writer(reference.field),
            )
        if isinstance(reference, ListValueReference):
            targets = reference.container if isinstance(reference.container, tuple) else reference.container.values
            return replace(
                reference,
                container=tuple(self.slot(target) for target in targets),
                value_encoder=ValueEncoders.prepare(reference.value_encoder, reference.field, self.types),
            )
        if isinstance(reference, RowListValueReference):
            if isinstance(reference.container, tuple):
                rows: tuple[tuple[Term | ParameterSlot, ...], ...] = reference.container
            else:
                rows = tuple(tuple(row.values) for row in reference.container.values)  # type: ignore[union-attr]
            return replace(
                reference,
                container=tuple(tuple(self.slot(target) for target in row) for row in rows),
                converters=tuple(self.types.get_lookup_writer(field) for field in reference.fields),
            )
        if isinstance(reference, ListParameterValueReference):
            source = reference.list_parameter
            if source is None:
                return reference
            return replace(
                reference,
                value_encoder=ValueEncoders.prepare(reference.value_encoder, reference.field, self.types),
                list_parameter=None,
                parameter_slot=self.slot(source.get_parameter_source()),
                parameter_encoder=source.get_parameter_encoder(),
            )
        if isinstance(reference, (RelatedValueReference, RelatedKeyValueReference)):
            nested = self.prepare(reference.reference)
            return None if nested is None else replace(reference, reference=nested)
        if isinstance(reference, CompositeKeyValueReference):
            components = tuple(self.prepare(component) for component in reference.component_references)
            if any(component is None for component in components):
                return None
            return replace(reference, component_references=components)  # type: ignore[arg-type]
        return None

    def prepare_inputs(
        self, sources: tuple[tuple[ValueReference, ...], ...]
    ) -> tuple[tuple[ValueReference, ...], ...] | None:
        """Prepare conversions in input order, keeping multi-target inputs together."""
        prepared: list[tuple[ValueReference, ...]] = []
        for references in sources:
            entries = []
            for reference in references:
                entry = self.prepare(reference)
                if entry is None:
                    return None
                entries.append(entry)
            prepared.append(tuple(entries))
        return tuple(prepared)
