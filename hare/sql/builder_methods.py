from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any, TypeVar, cast

TFunction = TypeVar("TFunction", bound=Callable[..., Any])
TReturnValue = TypeVar("TReturnValue")
TSelf = TypeVar("TSelf")


class BuilderMethods:
    """The decorators of the methods of the query-building classes: a builder method changes a
    copy of its object, and ``__getattr__`` of a class that is deep-copied leaves the copy protocol
    alone."""

    @staticmethod
    def builder(function: TFunction) -> TFunction:
        """Makes a method change a copy of its object - the object itself when it isn't immutable.
        The method returns the copy it changed (``self``), or something else (a join returns its
        joiner).

        Args:
            function: The method.

        Returns:
            The wrapped method, of the same type.
        """

        def copying_method(self: Any, *args: Any, **kwargs: Any) -> Any:
            self_copy = copy.copy(self) if getattr(self, "immutable", True) else self
            # The cached hash is dropped - the method is about to change the term.
            self_copy.__dict__.pop("_hash_cache", None)
            result = function(self_copy, *args, **kwargs)
            return self_copy if result is None else result

        return cast("TFunction", copying_method)

    @staticmethod
    def ignore_copy(function: Callable[[TSelf, str], TReturnValue]) -> Callable[[TSelf, str], TReturnValue]:
        """Makes ``__getattr__`` of a class that is deep-copied raise for the methods of the copy
        protocol - deepcopy probes for them, and a ``__getattr__`` answering would recurse forever.

        Args:
            function: The ``__getattr__``.

        Returns:
            The wrapped ``__getattr__``.
        """

        def copy_protocol_ignoring_getattr(self: TSelf, name: str) -> TReturnValue:
            if name in {"__copy__", "__deepcopy__", "__getstate__", "__setstate__", "__getnewargs__"}:
                raise AttributeError(f"'{self.__class__.__name__}' object has no attribute '{name}'")
            return function(self, name)

        return copy_protocol_ignoring_getattr
