from __future__ import annotations

import json

import pytest

from tests.artifacts.artifact_cache import ArtifactCache
from tests.artifacts.build_spec import BuildSpec
from tests.artifacts.constants import ARTIFACT_MANIFEST_NAME

pytestmark = pytest.mark.database_independent


@pytest.fixture
def artifact(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"source")
    spec = BuildSpec.create("library", {"source": source}, options=["-O2"], python_abi=True)
    cache = ArtifactCache(tmp_path / "cache")
    return cache, spec


@pytest.mark.parametrize("failure", ["build", "verify"])
def test_partial_or_unloadable_artifacts_are_never_published(artifact, failure):
    cache, spec = artifact

    def build(stage):
        (stage / "library").write_bytes(b"partial")
        assert cache.get(spec) is None
        if failure == "build":
            raise RuntimeError("build failed")

    def verify(stage):
        assert cache.get(spec) is None
        raise RuntimeError("load failed")

    with pytest.raises(RuntimeError):
        cache.prepare(spec, build, verify)
    assert cache.get(spec) is None
    assert list(cache.directory.iterdir()) == []


@pytest.mark.parametrize("damage", ["missing", "changed", "extra", "manifest"])
def test_damaged_published_artifacts_are_not_reused(artifact, damage):
    cache, spec = artifact
    built = []

    def build(stage):
        built.append(stage)
        (stage / "library").write_bytes(b"complete")

    def verify(stage):
        assert (stage / "library").read_bytes() == b"complete"

    first = cache.prepare(spec, build, verify)
    assert cache.prepare(spec, build, verify) == first
    assert len(built) == 1
    if damage == "missing":
        (first / "library").unlink()
    elif damage == "changed":
        (first / "library").write_bytes(b"truncated")
    elif damage == "extra":
        (first / "unexpected-library").write_bytes(b"foreign")
    else:
        (first / ARTIFACT_MANIFEST_NAME).write_text(json.dumps({"spec": None, "files": {}}), encoding="utf-8")
    assert cache.get(spec) is None
    second = cache.prepare(spec, build, verify)
    assert second != first
    assert first.is_dir()
    assert (second / "library").read_bytes() == b"complete"
    assert len(built) == 2


def test_source_options_and_exact_python_abi_change_artifact_identity(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_bytes(b"first")
    original = BuildSpec.create("library", {"source": source}, options=["-O2"], python_abi=True)
    changed_options = BuildSpec.create("library", {"source": source}, options=["-O0"], python_abi=True)
    source.write_bytes(b"second")
    changed_source = BuildSpec.create("library", {"source": source}, options=["-O2"], python_abi=True)
    abi = BuildSpec.get_python_abi() | {"Py_GIL_DISABLED": "different"}
    monkeypatch.setattr(BuildSpec, "get_python_abi", staticmethod(lambda: abi))
    changed_abi = BuildSpec.create("library", {"source": source}, options=["-O2"], python_abi=True)
    assert len({original.key, changed_options.key, changed_source.key, changed_abi.key}) == 4
