"""Public entry points import independently of modules already loaded by the test runner."""

import subprocess
import sys

import pytest


@pytest.mark.database_independent
@pytest.mark.parametrize(
    "module",
    ["hare.inspectdb", "hare.cli.context.command_context", "hare.dialects.clickhouse.functions", "hare.search"],
)
def test_public_entry_points_import_in_a_fresh_interpreter(module):
    result = subprocess.run([sys.executable, "-c", f"import {module}"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
