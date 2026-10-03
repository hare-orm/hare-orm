from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

from hare.query.expressions.value_refs.value_ref_types import RecordedValueRefs
from hare.query.queryset.query_spec import QuerySpec
from hare.query.statements.summary.exists_query import ExistsQuery

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model

TModel = TypeVar("TModel", bound="Model")


class ContainsQuery(ExistsQuery):
    def __init__(self, source: QuerySpec[Any], obj: TModel) -> None:
        """
        Args:
            source: The queryset checked.
            obj: The instance looked for among its rows.
        """
        super().__init__(source)
        self._obj = obj

    def _build_query(
        self,
        *,
        value_wrapper_refs: RecordedValueRefs | None = None,
    ) -> None:
        super()._build_query(value_wrapper_refs=value_wrapper_refs)
        # model._meta.pk/db_pk_column are only set for a single-column PK (None/"" for a
        # composite one) - build the criterion from every pk_attr_names column instead, so a
        # composite-PK model's .contains(obj) doesn't crash on self.model._meta.pk being None.
        pk_attr_names = self.model._meta.pk_attr_names
        pk_field_objs = [self.model._meta.fields_map[name] for name in pk_attr_names]
        obj_pk = self._obj.pk
        pk_values = obj_pk if isinstance(obj_pk, tuple) else (obj_pk,)
        table = self._effective_basetable()
        criterion = None
        for field_obj, value in zip(pk_field_objs, pk_values, strict=True):
            column = field_obj.source_field or field_obj.model_field_name
            eq = table[column].eq(self.dialect.types.get_db_value(field_obj, value, self._obj))
            criterion = eq if criterion is None else criterion & eq
        self.query = self.query.where(criterion)
