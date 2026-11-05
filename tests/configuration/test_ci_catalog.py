from __future__ import annotations

import pytest
import yaml

from tests.matrix.matrix_catalog import MatrixCatalog
from tests.matrix.paths import REPOSITORY_DIRECTORY

pytestmark = pytest.mark.database_independent


def test_every_ci_job_is_required_by_the_final_gate():
    workflow = yaml.safe_load((REPOSITORY_DIRECTORY / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    expected = MatrixCatalog.get("ci_gate.jobs")
    assert set(expected) == set(jobs) - {"ci-passed"}
    assert jobs["ci-passed"]["needs"] == expected
    matrices = MatrixCatalog.get_ci_matrices()
    assert set(matrices) == {name for name, job in jobs.items() if "matrix" in job.get("strategy", {})}
    for name in matrices:
        assert jobs[name]["strategy"]["matrix"] == "${{ fromJSON(needs.catalog.outputs.matrix)['" + name + "'] }}"
        assert "catalog" in jobs[name]["needs"]


@pytest.mark.parametrize("server", ["pgbouncer", "kafka"])
def test_every_pinned_server_version_has_an_image(server):
    assert set(MatrixCatalog.get(f"images.{server}")) == set(MatrixCatalog.get(f"versions.{server}"))
