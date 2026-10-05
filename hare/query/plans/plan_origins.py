from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar, TypeVar

T = TypeVar("T")

#: Where a value of a plan comes from - the identity of the object holding it, the attribute or key
#: it is held under, and its position in a sequence there.
type ValueOrigin = tuple[Any, ...]


class PlanOrigins:
    """Where each value of a query's plan comes from: a description lists, next to each value, the
    object holding it and the attribute or key it is held under, and the build records each
    reference to a value with the same origin. A plan binds a value into every reference of its
    origin, however many times the build resolved it.

    An object made again for each copy of a query - a copy of a query built into another one, the
    condition of a default scope, a filter call kept unbuilt - names the object it stands for in its
    ``_plan_origin``, set without a call: its identity is that object's, followed to the first one
    naming none. A condition the build derives from a value of another object - a filter across a
    relation, a key compared column by column - keeps the origin of that value (``derive()``).
    """

    #: Whether descriptions list the origin of each value - set only while a query records its
    #: plan (``describe()``); read as an attribute, so describing costs nothing more otherwise.
    records: ClassVar[bool] = False

    @staticmethod
    def get_token(owner: Any) -> int:
        """The identity a value held by ``owner`` comes from - the object a copy was made from.

        Args:
            owner: The object holding the value.

        Returns:
            The identity.
        """
        origin = getattr(owner, "_plan_origin", None)
        while origin is not None:
            owner = origin
            origin = getattr(owner, "_plan_origin", None)
        return id(owner)

    @staticmethod
    def get_value_origin(owner: Any, attribute: str, index: int | None = None) -> ValueOrigin:
        """The origin of a value held by ``owner`` under ``attribute`` - the value's own origin for a
        key of a derived condition.

        Args:
            owner: The object holding the value.
            attribute: The attribute or key the value is held under.
            index: The value's position in a sequence held under ``attribute``.

        Returns:
            The origin.
        """
        derived_origins: dict[str, ValueOrigin] | None = getattr(owner, "_value_origins", None)
        if derived_origins is not None:
            derived_origin = derived_origins.get(attribute)
            if derived_origin is not None:
                return derived_origin
        token = PlanOrigins.get_token(owner)
        return (token, attribute) if index is None else (token, attribute, index)

    @staticmethod
    def derive(condition: T, origins: dict[str, ValueOrigin]) -> T:
        """Gives each key of a condition the build derived from values of other objects the origin
        of its value.

        Args:
            condition: The derived condition.
            origins: The origin of each of its keys.

        Returns:
            ``condition``.
        """
        condition._value_origins = origins  # type: ignore[attr-defined]
        return condition

    @classmethod
    def describe(cls, describe: Callable[[], T]) -> T:
        """Describes with the origin of each value listed - a query built while describing (a
        subquery rendered to read its columns) describes itself the same way and leaves the origins
        listed.

        Args:
            describe: Makes the description.

        Returns:
            The description.
        """
        records = cls.records
        cls.records = True
        try:
            return describe()
        finally:
            cls.records = records
