from __future__ import annotations

import copy
import dataclasses
import json
from typing import Any

from hare.contrib.outbox import ChangeCapture
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.fields.generated_field import GeneratedField
from hare.models import Model
from hare.query.expressions import F, Window
from hare.query.functions import Coalesce, Greatest, JSONObject, Least, Max, Min
from hare.query.functions.window import Lag
from tests.contracts.constants import GENERATED_VALUE_FIELD_NAME, SAMPLED_FIELD_NAME
from tests.contracts.field_sample import FieldSample


class GeneratedFieldContract:
    """An operation on a sampled column and on an identity-generated copy of the same type."""

    def __init__(self, model: type[Model], sample: FieldSample) -> None:
        self.model = model
        self.sample = sample

    @staticmethod
    def with_generated_copy(sample: FieldSample) -> FieldSample:
        """Adds the generated column to the existing sample, retaining its values and requirements."""

        def make_fields():
            fields = sample.make_fields()
            output_field = copy.deepcopy(fields[SAMPLED_FIELD_NAME])
            fields[GENERATED_VALUE_FIELD_NAME] = GeneratedField(
                expression=RawSQLTerm(f'"{SAMPLED_FIELD_NAME}"'),
                output_field=output_field,
                null=output_field.null,
            )
            return fields

        return dataclasses.replace(sample, make_fields=make_fields)

    @staticmethod
    def canonical(value: Any) -> str:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)

    async def get_outcome(self, operation: str, field_name: str) -> tuple[tuple[str, Any], str | None]:
        """Returns a comparable value or exception type, with diagnostic text outside the comparison."""
        try:
            return ("value", self.canonical(await self.execute(operation, field_name))), None
        except Exception as error:
            return ("error", type(error)), str(error)

    async def execute(self, operation: str, field_name: str) -> Any:
        field = self.model._meta.fields_map[field_name]
        if operation == "json":
            return [
                ChangeCapture.get_row_values(self.model, {field_name: getattr(row, field_name)})[field_name]
                for row in await self.model.objects.order_by("id")
            ]
        if operation == "json_object":
            return await (
                self.model.objects.annotate(document=JSONObject(item=F(field_name)))
                .order_by("id")
                .values_list("document", flat=True)
            )
        if operation == "values":
            return [
                field.get_json_value(value)
                for value in await self.model.objects.order_by("id").values_list(field_name, flat=True)
            ]
        if operation == "annotation":
            values = await (
                self.model.objects.annotate(selected=F(field_name)).order_by("id").values_list("selected", flat=True)
            )
            return [field.get_json_value(value) for value in values]
        if operation == "coalesce_fractional":
            values = await (
                self.model.objects.annotate(selected=Coalesce(field_name, 0.5))
                .order_by("id")
                .values_list("selected", flat=True)
            )
            return [field.get_json_value(value) for value in values]
        if operation == "lag_fractional":
            values = await (
                self.model.objects.annotate(selected=Window(Lag(field_name, default=0.5), order_by=["id"]))
                .order_by("id")
                .values_list("selected", flat=True)
            )
            return [field.get_json_value(value) for value in values]
        if operation == "ordering":
            return await self.model.objects.order_by(field_name, "id").values_list("id", flat=True)
        if operation == "annotation_ordering":
            return await (
                self.model.objects.annotate(selected=F(field_name))
                .order_by("selected", "id")
                .values_list("id", flat=True)
            )
        if operation == "filter":
            return [
                await self.model.objects.filter(**{field_name: value}).order_by("id").values_list("id", flat=True)
                for value in self.sample.values
            ]
        if operation == "distinct":
            values = await self.model.objects.values_list(field_name, flat=True).distinct()
            return sorted(self.canonical(field.get_json_value(value)) for value in values)
        if operation in {"aggregate", "aggregate_empty"}:
            queryset = self.model.objects.all()
            if operation == "aggregate_empty":
                queryset = queryset.filter(id=-1)
            values = await queryset.aggregate(minimum=Min(field_name), maximum=Max(field_name))
            return {name: field.get_json_value(value) for name, value in values.items()}
        if operation == "greatest_least":
            rows = await (
                self.model.objects.annotate(
                    minimum=Least(field_name, field_name), maximum=Greatest(field_name, field_name)
                )
                .order_by("id")
                .values("minimum", "maximum")
            )
            return [{name: field.get_json_value(value) for name, value in row.items()} for row in rows]
        raise AssertionError(f"Unknown generated field operation: {operation}")
