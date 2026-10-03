"""LazyPattern - a dialect's regular expressions compile on first use, not on import."""

import copy
import re
import subprocess
import sys

from hare.utils.patterns import LazyPattern


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


def test_init_compiles_no_dialect_pattern():
    """Hare.init() imported the SQLite and PostgreSQL dialect constants, which compiled 36
    regular expressions for DDL, introspection and statement parsing - on every start."""
    script = (
        "import asyncio, re, sys\n"
        "compiled = []\n"
        "original_compile = re.compile\n"
        "def counting_compile(*args, **kwargs):\n"
        "    if sys._getframe(1).f_globals.get('__name__', '').startswith('hare'):\n"
        "        compiled.append(args[0])\n"
        "    return original_compile(*args, **kwargs)\n"
        "from hare import Hare, fields\n"
        "from hare.models import Model\n"
        "class Plain(Model):\n"
        "    id = fields.IntField(primary_key=True)\n"
        "    class Meta:\n"
        "        app = 'models'\n"
        "re.compile = counting_compile\n"
        "async def main():\n"
        "    await Hare.init(config={'connections': {'default': 'sqlite://:memory:'},\n"
        "        'apps': {'models': {'models': ['__main__'], 'default_connection': 'default'}}})\n"
        "    await Hare.close_connections()\n"
        "asyncio.run(main())\n"
        "print(len(compiled))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip().splitlines()[-1] == "0"
