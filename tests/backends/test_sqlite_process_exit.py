"""A program that ends - on an exception, say - without closing its SQLite connections still exits:
aiosqlite's worker thread doesn't hold the interpreter open."""

from __future__ import annotations

import subprocess  # nosec
import sys
import textwrap

FAILING_PROGRAM = textwrap.dedent(
    """
    import asyncio

    from hare import Hare, HareConfig, fields
    from hare.models import Model


    class Note(Model):
        title = fields.CharField(max_length=10)

        class Meta:
            app = "probe"


    async def main():
        await Hare.init(HareConfig.from_db_url("sqlite://:memory:", {"probe": ["__main__"]}))
        await Hare.generate_schemas()
        await Note.objects.create(title="open")
        raise RuntimeError("the program fails with its connection open")


    asyncio.run(main())
    """
)


def test_a_program_failing_with_an_open_connection_exits():
    completed = subprocess.run(  # nosec B603 - the running interpreter and a fixed program
        [sys.executable, "-c", FAILING_PROGRAM], capture_output=True, text=True, timeout=60, check=False
    )
    assert completed.returncode == 1
    assert "the program fails with its connection open" in completed.stderr
