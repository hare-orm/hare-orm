"""``hare makemigrations`` asks in the terminal whether a removed field or model declared like an added one
is renamed - not without a terminal, not with ``--no-input``; ``--rename`` answers beforehand."""

from __future__ import annotations

import io

import pytest

from hare.cli.commands import migration_commands
from tests.utils.cli_project_factory import project_factory  # noqa: F401

TITLE_MODEL = (
    "class Item(Model):\n    id = fields.IntField(primary_key=True)\n    title = fields.CharField(max_length=20)\n"
)
NAME_MODEL = (
    "class Item(Model):\n    id = fields.IntField(primary_key=True)\n    name = fields.CharField(max_length=20)\n"
)


class TerminalInput(io.StringIO):
    """Input typed in a terminal."""

    def isatty(self) -> bool:
        return True


@pytest.fixture
def terminal(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Answers yes to every question asked in the terminal, recording the questions."""
    questions: list[str] = []

    def answer_yes(prompt: str) -> str:
        questions.append(prompt)
        return "y"

    monkeypatch.setattr(migration_commands.sys, "stdin", TerminalInput())
    monkeypatch.setattr("builtins.input", answer_yes)
    return questions


@pytest.mark.asyncio
async def test_a_possible_rename_is_asked_in_the_terminal(project_factory, terminal: list[str]) -> None:  # noqa: F811
    project = await project_factory()
    project.write_models(TITLE_MODEL)
    await project.cli_ok("makemigrations")
    project.write_models(NAME_MODEL)

    await project.cli_ok("makemigrations")

    assert terminal == ["Was field 'title' of app.Item renamed to 'name'? [y/N] "]
    assert "ops.RenameField(" in project.read_latest_migration()


@pytest.mark.asyncio
async def test_no_input_asks_nothing_and_renames_nothing(project_factory, terminal: list[str]) -> None:  # noqa: F811
    project = await project_factory()
    project.write_models(TITLE_MODEL)
    await project.cli_ok("makemigrations")
    project.write_models(NAME_MODEL)

    output = await project.cli_ok("makemigrations", "--no-input")

    assert terminal == []
    assert "ops.RenameField(" not in project.read_latest_migration()
    assert "--rename app.Item.title=name" in output


@pytest.mark.asyncio
async def test_an_answer_given_with_rename_is_not_asked(project_factory, terminal: list[str]) -> None:  # noqa: F811
    project = await project_factory()
    project.write_models(TITLE_MODEL)
    await project.cli_ok("makemigrations")
    project.write_models(NAME_MODEL)

    await project.cli_ok("makemigrations", "--rename", "app.Item.title=name")

    assert terminal == []
    assert "ops.RenameField(" in project.read_latest_migration()


@pytest.mark.asyncio
async def test_without_a_terminal_nothing_is_asked(project_factory, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    asked: list[str] = []
    monkeypatch.setattr(migration_commands.sys, "stdin", io.StringIO())
    monkeypatch.setattr("builtins.input", asked.append)
    project = await project_factory()
    project.write_models(TITLE_MODEL)
    await project.cli_ok("makemigrations")
    project.write_models(NAME_MODEL)

    await project.cli_ok("makemigrations")

    assert asked == []
    assert "ops.RenameField(" not in project.read_latest_migration()


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["app.Item.title", "=name", "app.Item.title="])
async def test_a_rename_answer_without_both_names_is_refused(project_factory, answer: str) -> None:  # noqa: F811
    project = await project_factory()
    project.write_models(TITLE_MODEL)

    result = await project.cli("makemigrations", "--rename", answer)

    assert result.exit_code == 2
    assert "--rename takes OLD=NEW" in result.output
    assert project.migration_files() == []


@pytest.mark.asyncio
async def test_input_ended_in_the_terminal_is_no_answer(project_factory, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    def end_of_input(prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr(migration_commands.sys, "stdin", TerminalInput())
    monkeypatch.setattr("builtins.input", end_of_input)
    project = await project_factory()
    project.write_models(TITLE_MODEL)
    await project.cli_ok("makemigrations")
    project.write_models(NAME_MODEL)

    output = await project.cli_ok("makemigrations")

    assert "ops.RenameField(" not in project.read_latest_migration()
    assert "--rename app.Item.title=name" in output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("models", "answer"),
    [
        pytest.param(NAME_MODEL, "app.Item.titel=name", id="misspelt_field"),
        pytest.param(TITLE_MODEL, "app.Nope=Other", id="nothing_changed"),
    ],
)
async def test_a_rename_answer_that_renames_nothing_is_warned_about(project_factory, models: str, answer: str) -> None:  # noqa: F811
    """The answer was dropped silently - a field it meant was removed and added, its data lost."""
    project = await project_factory()
    project.write_models(TITLE_MODEL)
    await project.cli_ok("makemigrations")
    project.write_models(models)

    output = await project.cli_ok("makemigrations", "--no-input", "--rename", answer)

    assert f"The rename {answer} renamed nothing" in output
