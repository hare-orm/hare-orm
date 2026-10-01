from __future__ import annotations

from hare import fields
from hare.migrations.operations import CreateModel, RenameModel
from hare.migrations.state.apps import StateApps
from hare.migrations.state.project import State


def test_rename_model_unregisters_old_state() -> None:
    state = State(models={}, apps=StateApps())
    create = CreateModel(
        name="Author",
        fields=[("id", fields.IntField(primary_key=True))],
        options={"app": "blog", "table": "author", "pk_attr": "id"},
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
