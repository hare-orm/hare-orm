from __future__ import annotations

import math
import reprlib
import sys


class ShownValueRepr(reprlib.Repr):
    """The ``reprlib.Repr`` of the values an error message shows - an int past the interpreter's digit
    limit is shown as Python 3.13+ shows it, not left to raise ``ValueError`` as Python 3.12's ``reprlib``
    does."""

    def repr_int(self, x: int, level: int) -> str:
        try:
            return super().repr_int(x, level)
        except ValueError:
            digits = 1 + int(math.log10(abs(x)))
            return (
                f"<{x.__class__.__name__} instance with roughly {digits} digits "
                f"(limit at {sys.get_int_max_str_digits()}) at 0x{id(x):x}>"
            )
