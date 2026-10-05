"""``hare stubs``: each module of models as pyright reads it through its stub - the keys and values of
filters and writes checked, the managers, the fields of an abstract base and the rows typed."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys

import pytest

from tests.stubs.stub_configs import REPOSITORY_PATH

pytestmark = [
    pytest.mark.database_independent,
    pytest.mark.skipif(
        importlib.util.find_spec("pyright") is None or shutil.which("node") is None,
        reason="pyright and node aren't installed",
    ),
]

USAGE = """
from tests.contrib.factories_models import Member, Team


async def main() -> None:
    team = await Team.objects.get(name="core")
    await Member.objects.filter(team=team, name__icontains="a", team__name="core", team__in=[team, 1])
    names = await Member.objects.values_list("name", flat=True)
    reveal_type(names)
    reveal_type(team.name)
    await Member.objects.create(name="ann", email="a@b.c", team=team)
    await Member.objects.filter(nme="x")
    await Member.objects.filter(is_staff="yes")
    await Member.objects.create(name=1)
"""
MANAGERS_USAGE = """
from tests.stubs.stub_managers_models import Article


async def main(article: Article) -> None:
    reveal_type(Article.objects.published())
    reveal_type(Article.recent_ones.recent())
    reveal_type(article.note)
    await Article.objects.filter(titel="x")
"""


def run_pyright(project_path, stubs_path, usage: str) -> list[dict]:
    """Runs pyright on a module of the usage, reading the stubs.

    Returns:
        Its diagnostics.
    """
    shutil.copytree(stubs_path, project_path / "typings")
    (project_path / "usage.py").write_text(usage, encoding="utf-8")
    (project_path / "pyrightconfig.json").write_text(
        json.dumps({"stubPath": "typings", "include": ["usage.py"], "extraPaths": [str(REPOSITORY_PATH)]}),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [sys.executable, "-m", "pyright", "--outputjson", "--pythonpath", sys.executable],
        cwd=project_path,
        capture_output=True,
        text=True,
        check=False,
    )
    return json.loads(completed.stdout)["generalDiagnostics"]


def test_pyright_checks_queries_through_the_stubs(tmp_path, stubs_path):
    diagnostics = run_pyright(tmp_path, stubs_path, USAGE)
    errors = sorted(
        (diagnostic["range"]["start"]["line"], diagnostic["rule"])
        for diagnostic in diagnostics
        if diagnostic["severity"] == "error"
    )
    revealed = [diagnostic["message"] for diagnostic in diagnostics if diagnostic["severity"] == "information"]
    # The unknown key, the wrong value of a filter, the wrong value of a write - nothing else.
    assert errors == [(11, "reportCallIssue"), (12, "reportArgumentType"), (13, "reportArgumentType")]
    assert any("list[str]" in message for message in revealed)
    assert sum('"str"' in message for message in revealed) == 1


def test_pyright_reads_the_managers_and_the_abstract_fields_through_the_stubs(tmp_path, manager_stubs_path):
    diagnostics = run_pyright(tmp_path, manager_stubs_path, MANAGERS_USAGE)
    errors = [diagnostic["range"]["start"]["line"] for diagnostic in diagnostics if diagnostic["severity"] == "error"]
    revealed = [diagnostic["message"] for diagnostic in diagnostics if diagnostic["severity"] == "information"]
    # Only the unknown filter key.
    assert errors == [8]
    assert '"ArticleQuerySet"' in revealed[0]
    assert '"RecentQuerySet[Article]"' in revealed[1]
    assert '"str | None"' in revealed[2]
