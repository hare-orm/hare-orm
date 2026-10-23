"""LazyPattern - a dialect's regular expressions compile on first use, not on import."""

import copy
import re

import pytest

from hare.lazy_loading.lazy_pattern import LazyPattern

pytestmark = pytest.mark.database_independent


def test_compiles_on_first_use_and_then_calls_the_compiled_pattern_directly():
    pattern = LazyPattern(r"\bRETURNING\b", re.IGNORECASE)
    assert pattern.compiled is None
    assert pattern.search("insert ... returning id") is not None
    compiled = pattern.compiled
    assert isinstance(compiled, re.Pattern)
    assert compiled.flags & re.IGNORECASE
    # The compiled pattern's own bound method is kept - no lookup through LazyPattern again.
    assert vars(pattern)["search"].__self__ is compiled
    assert pattern.match("RETURNING") is not None
    assert pattern.fullmatch("x") is None
    assert [match.group() for match in pattern.finditer("returning RETURNING")] == ["returning", "RETURNING"]
    assert pattern.sub("-", "a returning b") == "a - b"
    assert pattern.pattern == r"\bRETURNING\b"


def test_a_copy_compiles_on_its_own():
    pattern = LazyPattern(r"\d+")
    copied = copy.copy(pattern)
    assert copied.match("12") is not None
    assert repr(pattern) == f"LazyPattern({pattern.source!r}, 0)"
