from __future__ import annotations

from hare.models import Model
from hare.query.queryset import QuerySet
from hare.query.queryset.relations.many_to_many_relation import ManyToManyRelation
from hare.query.queryset.relations.reverse_relation import ReverseRelation
from tests.contracts.constants import WRITE_METHOD_PATTERN


class WriteMethods:
    """The methods of hare's models, querysets and relations that write rows - found by their names among
    what each class declares itself, so a write method added to hare is found at once."""

    @staticmethod
    def get_write_methods() -> set[str]:
        """Every method that might write rows.

        Returns:
            Each as ``Class.method``.
        """
        classes = {
            "Model": Model,
            "QuerySet": QuerySet,
            "ReverseRelation": ReverseRelation,
            "ManyToManyRelation": ManyToManyRelation,
        }
        return {
            f"{class_name}.{member}"
            for class_name, class_ in classes.items()
            for member, value in vars(class_).items()
            if not member.startswith("_") and callable(value) and WRITE_METHOD_PATTERN.match(member)
        }
