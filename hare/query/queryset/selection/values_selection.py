from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace

from hare.query.enums import RowShape


@dataclass(frozen=True, slots=True)
class ValuesSelection:
    """What a ``.values()``/``.values_list()`` call selects - the rows of the queryset then come
    back in its shape instead of as model instances.

    Args:
        shape: The shape of a row.
        field_names: The names selected positionally - for ``.values_list()``, every selected name
            in order. Empty for every field and annotation of the model.
        renamed_fields: ``.values()``: each keyword name with the field or annotation name it
            selects - an expression passed as a keyword argument is an ``.alias()`` of the
            queryset under its keyword name.
        grouping_names: The annotations added after the call - an aggregate among them groups
            the rows by the selected fields, like Django's ``values(...).annotate(...)``.
        distinct_over_ordering_columns: A plain ``.distinct()`` runs over the selected columns
            plus every ordering column - the rows of the equivalent model queryset (the primary
            key query a queryset builds for its own rows).
        first_occurrence_rows_reversed: ``last()`` of a ``.distinct()`` ordered by a field it
            doesn't select - the first-occurrence rows are ordered backwards.
    """

    shape: RowShape
    field_names: tuple[str, ...] = ()
    renamed_fields: tuple[tuple[str, str], ...] = ()
    grouping_names: frozenset[str] = frozenset()
    distinct_over_ordering_columns: bool = False
    first_occurrence_rows_reversed: bool = False

    @property
    def selects_every_field(self) -> bool:
        """Whether no name is given - every field and annotation of the model is selected."""
        return not self.field_names and not self.renamed_fields

    def get_selected_names(self) -> list[str]:
        """The names given to the call - positional ones first, then the keyword names."""
        return [*self.field_names, *(name for name, _field_name in self.renamed_fields)]

    def with_added_names(self, added_names: Iterable[str], grouping_names: Iterable[str] = ()) -> ValuesSelection:
        """The selection after annotations are added to the queryset.

        Args:
            added_names: Annotations added with ``.annotate()`` - selected after the selected
                names (not by a ``flat=True`` call, which keeps its one column).
            grouping_names: Annotations added with ``.alias()`` - never selected.

        Returns:
            The selection.
        """
        added_names = list(added_names)
        selection = replace(self, grouping_names=self.grouping_names.union(added_names, grouping_names))
        if self.selects_every_field or self.shape is RowShape.FLAT:
            return selection
        if self.shape is RowShape.DICT:
            selected_names = set(self.get_selected_names())
            return replace(
                selection,
                renamed_fields=(
                    *self.renamed_fields,
                    *((name, name) for name in added_names if name not in selected_names),
                ),
            )
        return replace(
            selection, field_names=(*self.field_names, *(name for name in added_names if name not in self.field_names))
        )
