import pytest

from hare.migrations.state.model_state import ModelState


@pytest.fixture
def render_count(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    rendered_names: list[str] = []
    render = ModelState.render

    def counting_render(model_state: ModelState, *args: object, **kwargs: object) -> object:
        rendered_names.append(model_state.name)
        return render(model_state, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ModelState, "render", counting_render)
    return rendered_names
