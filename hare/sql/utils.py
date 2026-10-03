from collections.abc import Callable
from typing import TypeVar

TReturnValue = TypeVar("TReturnValue")
TSelf = TypeVar("TSelf")


def builder(func: Callable[..., TReturnValue]) -> Callable[..., TSelf | TReturnValue]:
    """Decorator for builder methods on Query and other query-building classes.

    Wraps a mutating method so it copies the instance first, keeping builder calls immutable.
    Returns the inner function's return value, or the copy if the inner function returns None.
    """
    import copy

    def _copy(self: TSelf, *args, **kwargs) -> TSelf | TReturnValue:
        self_copy = copy.copy(self) if getattr(self, "immutable", True) else self
        # The cached hash is dropped - func() is about to change the term.
        self_copy.__dict__.pop("_hash_cache", None)
        result = func(self_copy, *args, **kwargs)

        # Return self if the inner function returns None.  This way the inner function can return something
        # different (for example when creating joins, a different builder is returned).
        if result is None:
            return self_copy

        return result

    return _copy


def ignore_copy(func: Callable[[TSelf, str], TReturnValue]) -> Callable[[TSelf, str], TReturnValue]:
    """Decorator for __getattr__ on classes that get deepcopy'd.

    Prevents infinite recursion from deepcopy probing for magic methods. Any class implementing
    __getattr__ that is meant to be deepcopy'd should use this decorator.
    """

    def _getattr(self: TSelf, name: str) -> TReturnValue:
        if name in (
            "__copy__",
            "__deepcopy__",
            "__getstate__",
            "__setstate__",
            "__getnewargs__",
        ):
            raise AttributeError(f"'{self.__class__.__name__}' object has no attribute '{name}'")

        return func(self, name)

    return _getattr
