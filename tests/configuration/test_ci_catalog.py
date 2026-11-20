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
    assert set(matrices) == {
        name for name, job in jobs.items() if "matrix" in job.get("strategy", {}) or "matrix" in job.get("with", {})
    }
    for name in matrices:
        job = jobs[name]
        catalog_matrix = "fromJSON(needs.catalog.outputs.matrix)['" + name + "']"
        if "uses" in job:
            # A reusable workflow takes its matrix as an input.
            assert job["with"]["matrix"] == "${{ toJSON(" + catalog_matrix + ") }}"
        else:
            assert job["strategy"]["matrix"] == "${{ " + catalog_matrix + " }}"
        assert "catalog" in job["needs"]


def test_ci_builds_the_wheels_of_release_platforms_on_every_os():
    ci_platforms = MatrixCatalog.get("ci.platform-wheels.platforms")
    included = MatrixCatalog.get_ci_matrices()["platform-wheels"]["include"]
    assert [platform["platform"] for platform in included] == ci_platforms
    assert {platform["runner"].split("-")[0] for platform in included} == {"ubuntu", "macos", "windows"}


@pytest.mark.parametrize("server", ["pgbouncer", "kafka"])
def test_every_pinned_server_version_has_an_image(server):
    assert set(MatrixCatalog.get(f"images.{server}")) == set(MatrixCatalog.get(f"versions.{server}"))
