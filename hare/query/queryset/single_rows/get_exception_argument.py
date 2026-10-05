from __future__ import annotations

from hare.query.enums import GetException

#: What an exception parameter of ``get()`` takes: ``GetException.STANDARD``, an exception class or
#: instance to raise instead, or None for no exception.
type GetExceptionArgument = type[BaseException] | BaseException | GetException | None
