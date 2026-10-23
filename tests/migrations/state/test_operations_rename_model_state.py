from __future__ import annotations

import pytest

from hare import fields
from hare.migrations.operations import CreateModel, RenameModel
from hare.migrations.state.state import State

pytestmark = pytest.mark.database_independent


def test_rename_model_unregisters_old_state() -> None:
    state = State()
    create = CreateModel(
        name="Author",
        fields=[("id", fields.IntField(primary_key=True))],
        options={"app": "blog", "table": "author", "primary_key_attribute": "id"},
        bases=["Model"],
    )
    create.state_forward("blog", state)
    assert ("blog", "Author") in state.models
    assert "Author" in state.apps.apps.get("blog", {})

    rename = RenameModel(old_name="Author", new_name="Writer")
    rename.state_forward("blog", state)

    assert ("blog", "Author") not in state.models
    assert "Author" not in state.apps.apps.get("blog", {})
    assert ("blog", "Writer") in state.models
    assert "Writer" in state.apps.apps.get("blog", {})
