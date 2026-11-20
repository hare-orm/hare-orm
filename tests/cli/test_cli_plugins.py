from __future__ import annotations

import importlib.metadata
import textwrap
from pathlib import Path

import pytest

from hare.cli import hare_cli as cli_module
from hare.cli.plugins.cli_command_registry import CLICommandRegistry
from hare.core.config import HareConfig
from hare.core.hare_context import HareContext
from tests.utils.cli_commands import CliCommands
from tests.utils.project_imports import ProjectImports
from tests.utils.project_sources import ProjectSources

PLUGIN_MODULE_SOURCE = textwrap.dedent(
    """
    from hare.cli.plugins import CLICommand, CLIError, CommandContext


    class GreetCommand(CLICommand):
        name = "greet"
        help = "Say hello to the configured apps."

        def add_arguments(self, parser):
            if hasattr(self, "parser"):
                raise RuntimeError("arguments were declared twice")
            self.parser = parser
            parser.description = "Greet the configured applications."
            parser.set_defaults(greeting_prefix="hello")
            parser.add_argument("--who", default="world")

        async def run(self, ctx, args):
            assert self.parser.prog.endswith(" greet")
            config = CommandContext.load_config(ctx)
            print(f"{args.greeting_prefix} {args.who} from {sorted(config.apps)}")
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


    class ArgumentNamesCommand(CLICommand):
        name = "argument-names"

        def add_arguments(self, parser):
            parser.add_argument("func")
            parser.add_argument("config")
            parser.add_argument("command")
            parser.add_argument("--callback", dest="handler", default="payload")

        async def run(self, ctx, args):
            print(f"{args.func}|{args.config}|{args.command}|{args.handler}|{ctx.config}")
            return 7


    class CaptureFlagCommand(CLICommand):
        name = "capture-flag"

        def add_arguments(self, parser):
            parser.add_argument("-custom", "-count", "--custom", dest="enabled", action="store_true")

        async def run(self, ctx, args):
            print(f"{args.enabled}|{ctx.config}")
            return 8


    class CaptureModelsCommand(CLICommand):
        name = "capture-models"

        async def run(self, ctx, args):
            config = CommandContext.load_config(ctx)
            print(list(config.apps["models"].models))
    """
)


def _write_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_commands: list[str] | None) -> str:
    sources = ProjectSources(tmp_path)
    sources.write_module("cli_plugin_commands", PLUGIN_MODULE_SOURCE)
    sources.write_module("cli_plugin_models", "")
    cli_section = f', "cli": {{"commands": {cli_commands!r}}}' if cli_commands is not None else ""
    sources.write_module(
        "cli_plugin_settings",
        'HARE_ORM = {"connections": {"default": "sqlite+aiosqlite://:memory:"}, '
        f'"apps": {{"models": {{"models": ["cli_plugin_models"]}}}}{cli_section}}}\n',
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.chdir(tmp_path)
    return "cli_plugin_settings.HARE_ORM"


def _use_entry_points(monkeypatch: pytest.MonkeyPatch, values: dict[str, str]) -> None:
    entry_points = [
        importlib.metadata.EntryPoint(name=name, value=value, group="hare.cli") for name, value in values.items()
    ]
    monkeypatch.setattr(CLICommandRegistry, "get_entry_points", staticmethod(lambda: entry_points))


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_config_listed_command_runs_with_its_arguments_and_exit_code(tmp_path, monkeypatch):
    _use_entry_points(monkeypatch, {})
    config = _write_project(tmp_path, monkeypatch, ["cli_plugin_commands:GreetCommand"])

    result = await CliCommands.run(["-c", config, "greet", "--who", "team"], project=tmp_path)

    assert result.exit_code == 3
    assert result.stdout.strip() == "hello team from ['models']"
    assert result.stderr == ""


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_plugin_argument_destinations_do_not_replace_execution_metadata(tmp_path, monkeypatch):
    _use_entry_points(monkeypatch, {})
    config = _write_project(tmp_path, monkeypatch, ["cli_plugin_commands:ArgumentNamesCommand"])
    result = await CliCommands.run(
        ["-c", config, "argument-names", "count", "plugin.cfg", "payload-command"], project=tmp_path
    )
    assert result.exit_code == 7
    assert result.stdout.strip() == f"count|plugin.cfg|payload-command|payload|{config}"
    assert result.stderr == ""


@pytest.mark.database_independent
def test_synchronous_dispatch_also_keeps_plugin_arguments_separate(tmp_path, monkeypatch, capsys):
    _use_entry_points(monkeypatch, {})
    config = _write_project(tmp_path, monkeypatch, ["cli_plugin_commands:ArgumentNamesCommand"])
    assert cli_module.HareCLI.run_cli(["-c", config, "argument-names", "count", "plugin.cfg", "payload-command"]) == 7
    captured = capsys.readouterr()
    assert captured.out.strip() == f"count|plugin.cfg|payload-command|payload|{config}"
    assert captured.err == ""


@pytest.mark.database_independent
@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["-custom", "-count", "--custom"])
async def test_plugin_option_prefixes_are_not_global_config_options(tmp_path, monkeypatch, flag):
    _use_entry_points(monkeypatch, {})
    config = _write_project(tmp_path, monkeypatch, ["cli_plugin_commands:CaptureFlagCommand"])
    result = await CliCommands.run(["--config", config, "capture-flag", flag], project=tmp_path)
    assert result.exit_code == 8
    assert result.stdout.strip() == f"True|{config}"
    assert result.stderr == ""


@pytest.mark.database_independent
@pytest.mark.asyncio
@pytest.mark.parametrize("iterable", ["(name for name in ['cli_plugin_models'])", "iter(['cli_plugin_models'])"])
async def test_discovery_and_execution_share_a_config_with_one_pass_model_modules(tmp_path, monkeypatch, iterable):
    _use_entry_points(monkeypatch, {})
    config = _write_project(tmp_path, monkeypatch, ["cli_plugin_commands:CaptureModelsCommand"])
    settings = tmp_path / "cli_plugin_settings.py"
    settings.write_text(
        settings.read_text(encoding="utf-8") + f"HARE_ORM['apps']['models']['models'] = {iterable}\n",
        encoding="utf-8",
    )
    result = await CliCommands.run(["-c", config, "capture-models"], project=tmp_path)
    assert result.exit_code == 0
    assert result.stdout.strip() == "['cli_plugin_models']"
    assert result.stderr == ""


@pytest.mark.database_independent
@pytest.mark.asyncio
@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
async def test_stubs_keeps_raw_config_model_generators_through_discovery(tmp_path, monkeypatch, composite):
    _use_entry_points(monkeypatch, {})
    config = _write_project(tmp_path, monkeypatch, ["cli_plugin_commands:ExportCommand"])
    model_source = (
        "from hare import Model, fields\n\nclass Entry(Model):\n"
        f"    number = fields.IntField(primary_key={not composite!r}, generated=False)\n"
        "    title = fields.TextField()\n"
    )
    if composite:
        model_source += '    region = fields.IntField()\n    pk = fields.CompositePrimaryKey("region", "number")\n'
    (tmp_path / "cli_plugin_models.py").write_text(model_source, encoding="utf-8")
    settings = tmp_path / "cli_plugin_settings.py"
    settings.write_text(
        settings.read_text(encoding="utf-8")
        + "HARE_ORM['apps']['models']['models'] = (name for name in ['cli_plugin_models'])\n",
        encoding="utf-8",
    )
    output = tmp_path / "typings"
    result = await CliCommands.run(["-c", config, "stubs", "--output", str(output)], project=tmp_path)
    assert result.exit_code == 0, result.stderr
    stub = output / "cli_plugin_models.pyi"
    assert stub.is_file()
    stub_source = stub.read_text(encoding="utf-8")
    assert "class Entry(" in stub_source
    assert "title" in stub_source


@pytest.mark.database_independent
@pytest.mark.asyncio
@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
@pytest.mark.parametrize("secondary_exists", [False, True], ids=["missing-alias", "other-rows"])
async def test_stubs_keeps_typing_bindings_only_for_the_generation_scope(
    tmp_path, monkeypatch, composite, secondary_exists
):
    _use_entry_points(monkeypatch, {})
    source = (
        "from hare import Model, fields\nclass Entry(Model):\n"
        f"    number = fields.IntField(primary_key={not composite!r}, generated=False)\n"
        "    label = fields.TextField()\n"
    )
    if composite:
        source += '    region = fields.IntField(default=7)\n    pk = fields.CompositePrimaryKey("region", "number")\n'
    source += '    class Meta:\n        table = "stubs_active_entry"\n'
    sources = ProjectSources(tmp_path)
    sources.write_module("active_typing_models", source)
    sources.write_module(
        "typing_config",
        "HARE_ORM = {'connections': {'secondary': 'sqlite+aiosqlite://:memory:'}, "
        "'apps': {'books': {'models': ['active_typing_models'], 'default_connection': 'secondary'}}}\n",
    )
    connections = {"primary": "sqlite+aiosqlite://:memory:"}
    if secondary_exists:
        connections["secondary"] = "sqlite+aiosqlite://:memory:"
    with ProjectImports(tmp_path).activate():
        async with HareContext() as owner:
            await owner.init(
                config={
                    "connections": connections,
                    "apps": {"books": {"models": ["active_typing_models"], "default_connection": "primary"}},
                }
            )
            model = owner.apps["books"]["Entry"]
            await owner.generate_schemas()
            await model.objects.create(number=1, label="kept")
            if secondary_exists:
                await owner.get_connection("secondary").execute_script(
                    "CREATE TABLE stubs_active_entry(number INTEGER, region INTEGER, label TEXT);"
                    "INSERT INTO stubs_active_entry VALUES(2,7,'other');"
                )
            assert await model.objects.values_list("label", flat=True) == ["kept"]
            output = tmp_path / "typings"
            assert (
                await cli_module.HareCLI.run_cli_async(
                    ["-c", "typing_config.HARE_ORM", "stubs", "--output", str(output)]
                )
                == 0
            )
            assert (output / "active_typing_models.pyi").is_file()
            assert HareContext.get_current() is owner
            assert HareContext.require_current().apps.get_binding(model).default_connection == "primary"
            assert await model.objects.values_list("label", flat=True) == ["kept"]
            if secondary_exists:
                assert await model.objects.using("secondary").values_list("label", flat=True) == ["other"]


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_entry_point_command_runs_without_any_config(tmp_path, monkeypatch):
    _write_project(tmp_path, monkeypatch, None)
    _use_entry_points(monkeypatch, {"export": "cli_plugin_commands:ExportCommand"})

    result = await CliCommands.run(["export"], project=tmp_path)

    assert result.exit_code == 0
    assert result.stdout.strip() == "exported"


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_help_lists_plugin_commands_next_to_built_in_ones(tmp_path, monkeypatch):
    config = _write_project(tmp_path, monkeypatch, ["cli_plugin_commands:GreetCommand"])
    _use_entry_points(monkeypatch, {"export": "cli_plugin_commands:ExportCommand"})

    result = await CliCommands.run(["-c", config, "--help"], project=tmp_path)

    assert result.exit_code == 0
    for text in ("greet", "Say hello to the configured apps.", "export", "migrate"):
        assert text in result.stdout


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_plugin_error_is_reported_like_a_built_in_command_error(tmp_path, monkeypatch):
    _write_project(tmp_path, monkeypatch, None)
    _use_entry_points(monkeypatch, {"explode": "cli_plugin_commands:FailingCommand"})

    result = await CliCommands.run(["explode"], project=tmp_path)

    assert result.exit_code == 1
    assert "it went wrong" in result.stderr


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_plugin_cannot_replace_a_built_in_command(tmp_path, monkeypatch):
    _write_project(tmp_path, monkeypatch, None)
    _use_entry_points(monkeypatch, {"heads": "cli_plugin_commands:ShadowingCommand"})

    handlers = {}
    parser = cli_module.ArgParserBuilder.build_parser(
        CLICommandRegistry.discover(cli_module.CLIContext(None)), command_handlers=handlers
    )

    assert parser.parse_args(["heads"]).command == "heads"
    assert handlers["heads"].__qualname__ != "ShadowingCommand.run"


@pytest.mark.database_independent
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

    result = await CliCommands.run(["-c", config, "greet"], project=tmp_path)

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
        ("cli_plugin_commands:NoSuchClass", "AttributeError"),
    ],
)
async def test_unloadable_entry_point_is_skipped_and_the_cli_keeps_working(
    tmp_path, monkeypatch, entry_point_value, expected_warning
):
    _write_project(tmp_path, monkeypatch, None)
    _use_entry_points(monkeypatch, {"bad": entry_point_value, "export": "cli_plugin_commands:ExportCommand"})

    result = await CliCommands.run(["export"], project=tmp_path)

    assert result.exit_code == 0
    assert result.stdout.strip() == "exported"
    assert "skipping CLI command from entry point 'bad'" in result.stderr
    assert expected_warning in result.stderr


@pytest.mark.database_independent
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reference",
    ["cli_plugin_commands.GreetCommand", "cli_plugin_commands:", ":GreetCommand"],
)
async def test_malformed_config_reference_is_skipped_with_a_warning(tmp_path, monkeypatch, reference):
    _use_entry_points(monkeypatch, {})
    config = _write_project(tmp_path, monkeypatch, [reference])

    result = await CliCommands.run(["-c", config, "--help"], project=tmp_path)

    assert result.exit_code == 0
    assert f"skipping CLI command from config cli.commands entry {reference!r}" in result.stderr
    assert "package.module:ClassName" in result.stderr


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_command_whose_arguments_fail_to_declare_is_not_half_registered(tmp_path, monkeypatch):
    _write_project(tmp_path, monkeypatch, None)
    _use_entry_points(monkeypatch, {"broken-args": "cli_plugin_commands:BrokenArgumentsCommand"})

    help_result = await CliCommands.run(["--help"], project=tmp_path)
    run_result = await CliCommands.run(["broken-args"], project=tmp_path)

    assert "skipping CLI command 'broken-args'" in help_result.stderr
    assert "cannot declare arguments" in help_result.stderr
    assert "broken-args" not in help_result.stdout
    assert run_result.exit_code == 2


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_missing_config_does_not_hide_installed_plugin_commands(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HARE_ORM_CONFIG", raising=False)
    (tmp_path / "cli_plugin_commands.py").write_text(PLUGIN_MODULE_SOURCE, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    _use_entry_points(monkeypatch, {"export": "cli_plugin_commands:ExportCommand"})

    result = await CliCommands.run(["export"], project=tmp_path)

    assert result.exit_code == 0
    assert result.stdout.strip() == "exported"


@pytest.mark.database_independent
def test_config_cli_section_round_trips_and_is_validated():
    base = {
        "connections": {"default": "sqlite+aiosqlite://:memory:"},
        "apps": {"models": {"models": ["cli_plugin_models"]}},
    }

    config = HareConfig.from_dict({**base, "cli": {"commands": ["package.module:Command"]}})
    assert config.cli.commands == ["package.module:Command"]
    assert config.to_dict()["cli"] == {"commands": ["package.module:Command"]}
    assert HareConfig.from_dict(base).cli is None
    assert "cli" not in HareConfig.from_dict(base).to_dict()
