from __future__ import annotations

import contextlib
import importlib.metadata
import io
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from hare.cli import hare_cli as cli_module
from hare.cli.plugins import CLICommandRegistry
from hare.core.config import HareConfig
from hare.exceptions import ConfigurationError

PLUGIN_MODULE_SOURCE = textwrap.dedent(
    """
    from hare.cli.plugins import CLICommand, CLIError, CommandContext


    class GreetCommand(CLICommand):
        name = "greet"
        help = "Say hello to the configured apps."

        def add_arguments(self, parser):
            parser.add_argument("--who", default="world")

        async def run(self, ctx, args):
            config = CommandContext.load_config(ctx)
            print(f"hello {args.who} from {sorted(config.apps)}")
            return 3


    class ExportCommand(CLICommand):
        name = "export"
        help = "Export something."

        async def run(self, ctx, args):
            print("exported")


    class FailingCommand(CLICommand):
        name = "explode"

        async def run(self, ctx, args):
            raise CLIError("it went wrong")


    class ShadowingCommand(CLICommand):
        name = "heads"

        async def run(self, ctx, args):
            print("shadowed")


    class BrokenArgumentsCommand(CLICommand):
        name = "broken-args"

        def add_arguments(self, parser):
            raise RuntimeError("cannot declare arguments")

        async def run(self, ctx, args):
            print("never")


    class NotACommand:
        name = "plain"
    """
)


def _write_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_commands: list[str] | None) -> str:
    (tmp_path / "cli_plugin_commands.py").write_text(PLUGIN_MODULE_SOURCE, encoding="utf-8")
    (tmp_path / "cli_plugin_models.py").write_text("", encoding="utf-8")
    cli_section = f', "cli": {{"commands": {cli_commands!r}}}' if cli_commands is not None else ""
    (tmp_path / "cli_plugin_settings.py").write_text(
        'HARE_ORM = {"connections": {"default": "sqlite://:memory:"}, '
        f'"apps": {{"models": {{"models": ["cli_plugin_models"]}}}}{cli_section}}}\n',
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.chdir(tmp_path)
    return "cli_plugin_settings.HARE_ORM"


def _use_entry_points(monkeypatch: pytest.MonkeyPatch, values: dict[str, str]) -> None:
    entry_points = [
        importlib.metadata.EntryPoint(name=name, value=value, group="hare.cli") for name, value in values.items()
    ]
    monkeypatch.setattr(CLICommandRegistry, "get_entry_points", staticmethod(lambda: entry_points))


async def _run_cli(args: list[str]) -> SimpleNamespace:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        exit_code = await cli_module.HareCLI.run_cli_async(args)
    return SimpleNamespace(exit_code=exit_code, stdout=stdout.getvalue(), stderr=stderr.getvalue())


@pytest.mark.asyncio
async def test_config_listed_command_runs_with_its_arguments_and_exit_code(tmp_path, monkeypatch):
    _use_entry_points(monkeypatch, {})
    config = _write_project(tmp_path, monkeypatch, ["cli_plugin_commands:GreetCommand"])

    result = await _run_cli(["-c", config, "greet", "--who", "team"])

    assert result.exit_code == 3
    assert result.stdout.strip() == "hello team from ['models']"
    assert result.stderr == ""


@pytest.mark.asyncio
async def test_entry_point_command_runs_without_any_config(tmp_path, monkeypatch):
    _write_project(tmp_path, monkeypatch, None)
    _use_entry_points(monkeypatch, {"export": "cli_plugin_commands:ExportCommand"})

    result = await _run_cli(["export"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "exported"


@pytest.mark.asyncio
async def test_help_lists_plugin_commands_next_to_built_in_ones(tmp_path, monkeypatch):
    config = _write_project(tmp_path, monkeypatch, ["cli_plugin_commands:GreetCommand"])
    _use_entry_points(monkeypatch, {"export": "cli_plugin_commands:ExportCommand"})

    result = await _run_cli(["-c", config, "--help"])

    assert result.exit_code == 0
    for text in ("greet", "Say hello to the configured apps.", "export", "migrate"):
        assert text in result.stdout


@pytest.mark.asyncio
async def test_plugin_error_is_reported_like_a_built_in_command_error(tmp_path, monkeypatch):
    _write_project(tmp_path, monkeypatch, None)
    _use_entry_points(monkeypatch, {"explode": "cli_plugin_commands:FailingCommand"})

    result = await _run_cli(["explode"])

    assert result.exit_code == 1
    assert "it went wrong" in result.stderr


@pytest.mark.asyncio
async def test_plugin_cannot_replace_a_built_in_command(tmp_path, monkeypatch):
    _write_project(tmp_path, monkeypatch, None)
    _use_entry_points(monkeypatch, {"heads": "cli_plugin_commands:ShadowingCommand"})

    parser = cli_module.ArgParserBuilder.build_parser(CLICommandRegistry.discover(cli_module.CLIContext(None)))

    assert parser.parse_args(["heads"]).func is not None
    assert parser.parse_args(["heads"]).func.__qualname__ != "ShadowingCommand.run"


@pytest.mark.asyncio
async def test_name_conflicts_are_warned_about_and_the_later_command_skipped(tmp_path, monkeypatch):
    config = _write_project(tmp_path, monkeypatch, ["cli_plugin_commands:GreetCommand"])
    _use_entry_points(
        monkeypatch,
        {
            "greet": "cli_plugin_commands:GreetCommand",
            "heads": "cli_plugin_commands:ShadowingCommand",
        },
    )

    result = await _run_cli(["-c", config, "greet"])

    assert result.exit_code == 3
    assert "skipping CLI command 'heads'" in result.stderr
    assert "already taken by hare itself" in result.stderr
    assert "skipping CLI command 'greet' from config cli.commands entry" in result.stderr
    assert "already taken by entry point 'greet'" in result.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entry_point_value", "expected_warning"),
    [
        ("cli_plugin_missing_module:Anything", "ModuleNotFoundError"),
        ("cli_plugin_commands:NotACommand", "is not a CLICommand subclass"),
        ("cli_plugin_commands:NoSuchClass", "AttributeError"),
    ],
)
async def test_unloadable_entry_point_is_skipped_and_the_cli_keeps_working(
    tmp_path, monkeypatch, entry_point_value, expected_warning
):
    _write_project(tmp_path, monkeypatch, None)
    _use_entry_points(monkeypatch, {"bad": entry_point_value, "export": "cli_plugin_commands:ExportCommand"})

    result = await _run_cli(["export"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "exported"
    assert "skipping CLI command from entry point 'bad'" in result.stderr
    assert expected_warning in result.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reference",
    ["cli_plugin_commands.GreetCommand", "cli_plugin_commands:", ":GreetCommand"],
)
async def test_malformed_config_reference_is_skipped_with_a_warning(tmp_path, monkeypatch, reference):
    _use_entry_points(monkeypatch, {})
    config = _write_project(tmp_path, monkeypatch, [reference])

    result = await _run_cli(["-c", config, "--help"])

    assert result.exit_code == 0
    assert f"skipping CLI command from config cli.commands entry {reference!r}" in result.stderr
    assert "package.module:ClassName" in result.stderr


@pytest.mark.asyncio
async def test_command_whose_arguments_fail_to_declare_is_not_half_registered(tmp_path, monkeypatch):
    _write_project(tmp_path, monkeypatch, None)
    _use_entry_points(monkeypatch, {"broken-args": "cli_plugin_commands:BrokenArgumentsCommand"})

    help_result = await _run_cli(["--help"])
    run_result = await _run_cli(["broken-args"])

    assert "skipping CLI command 'broken-args'" in help_result.stderr
    assert "cannot declare arguments" in help_result.stderr
    assert "broken-args" not in help_result.stdout
    assert run_result.exit_code == 2


@pytest.mark.asyncio
async def test_missing_config_does_not_hide_installed_plugin_commands(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HARE_ORM_CONFIG", raising=False)
    (tmp_path / "cli_plugin_commands.py").write_text(PLUGIN_MODULE_SOURCE, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    _use_entry_points(monkeypatch, {"export": "cli_plugin_commands:ExportCommand"})

    result = await _run_cli(["export"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "exported"


def test_config_cli_section_round_trips_and_is_validated():
    base = {"connections": {"default": "sqlite://:memory:"}, "apps": {"models": {"models": ["cli_plugin_models"]}}}

    config = HareConfig.from_dict({**base, "cli": {"commands": ["package.module:Command"]}})
    assert config.cli.commands == ["package.module:Command"]
    assert config.to_dict()["cli"] == {"commands": ["package.module:Command"]}
    assert HareConfig.from_dict(base).cli is None
    assert "cli" not in HareConfig.from_dict(base).to_dict()

    for bad_cli_section in ("package.module:Command", {"commands": "package.module:Command"}, {"commands": [""]}):
        with pytest.raises(ConfigurationError):
            HareConfig.from_dict({**base, "cli": bad_cli_section})
