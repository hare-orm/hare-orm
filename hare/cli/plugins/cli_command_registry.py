from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import sys

from hare.cli.constants import CLI_COMMAND_ENTRY_POINT_GROUP, CLI_COMMAND_REFERENCE_SEPARATOR
from hare.cli.context.cli_context import CLIContext
from hare.cli.context.command_context import CommandContext
from hare.cli.plugins.cli_command import CLICommand
from hare.cli.plugins.loaded_cli_command import LoadedCLICommand
from hare.exceptions import ConfigurationError


class CLICommandRegistry:
    """Finds plugin commands and adds them to the `hare` argument parser."""

    @staticmethod
    def get_entry_points() -> list[importlib.metadata.EntryPoint]:
        """Returns the entry points registered under ``CLI_COMMAND_ENTRY_POINT_GROUP``."""
        return list(importlib.metadata.entry_points(group=CLI_COMMAND_ENTRY_POINT_GROUP))

    @staticmethod
    def warn(message: str) -> None:
        """Reports a plugin problem without stopping the CLI."""
        print(f"hare: warning: {message}", file=sys.stderr)

    @staticmethod
    def instantiate(command_class: object, source: str) -> LoadedCLICommand:
        """Validates a loaded plugin object and creates the command.

        Args:
            command_class: The object the plugin reference points at.
            source: Where it came from.

        Returns:
            The loaded command.

        Raises:
            TypeError: If the object isn't a ``CLICommand`` subclass with a valid ``name``.
        """
        if not isinstance(command_class, type) or not issubclass(command_class, CLICommand):
            raise TypeError(f"{command_class!r} is not a CLICommand subclass")
        name = getattr(command_class, "name", None)
        if not isinstance(name, str) or not name or name.startswith("-"):
            raise TypeError(f"{command_class.__qualname__}.name must be a non-empty subcommand name")
        return LoadedCLICommand(command=command_class(), source=source)

    @staticmethod
    def load_entry_point_commands() -> list[LoadedCLICommand]:
        """Loads every command installed packages register - a broken one is reported and skipped."""
        loaded_commands: list[LoadedCLICommand] = []
        for entry_point in CLICommandRegistry.get_entry_points():
            distribution = getattr(entry_point, "dist", None)
            package = f"package {distribution.name!r}, " if distribution is not None else ""
            source = f"{package}entry point {entry_point.name!r} = {entry_point.value!r}"
            try:
                loaded_commands.append(CLICommandRegistry.instantiate(entry_point.load(), source))
            except Exception as exc:
                CLICommandRegistry.warn(f"skipping CLI command from {source}: {exc!r}")
        return loaded_commands

    @staticmethod
    def import_command_reference(reference: str) -> object:
        """Imports the object a "package.module:ClassName" reference points at.

        Args:
            reference: The command reference.

        Returns:
            The referenced object.

        Raises:
            ConfigurationError: If the reference isn't "module:attribute".
        """
        module_path, separator, attribute_path = reference.partition(CLI_COMMAND_REFERENCE_SEPARATOR)
        if not separator or not module_path or not attribute_path:
            raise ConfigurationError(f"expected 'package.module{CLI_COMMAND_REFERENCE_SEPARATOR}ClassName'")
        target: object = importlib.import_module(module_path)
        for attribute in attribute_path.split("."):
            target = getattr(target, attribute)
        return target

    @staticmethod
    def load_config_commands(ctx: CLIContext) -> list[LoadedCLICommand]:
        """Loads the commands the project's config lists under ``cli.commands``.

        A config that can't be found or loaded contributes nothing here - the command actually
        being run reports that problem itself.
        """
        try:
            config = CommandContext.load_config(ctx)
        except Exception:
            return []
        loaded_commands: list[LoadedCLICommand] = []
        for reference in config.cli.commands if config.cli else ():
            source = f"config cli.commands entry {reference!r}"
            try:
                command_class = CLICommandRegistry.import_command_reference(reference)
                loaded_commands.append(CLICommandRegistry.instantiate(command_class, source))
            except Exception as exc:
                CLICommandRegistry.warn(f"skipping CLI command from {source}: {exc!r}")
        return loaded_commands

    @staticmethod
    def discover(ctx: CLIContext) -> list[LoadedCLICommand]:
        """Loads commands from installed packages, then from the project's config."""
        return [*CLICommandRegistry.load_entry_point_commands(), *CLICommandRegistry.load_config_commands(ctx)]

    @staticmethod
    def register(
        subparsers: argparse._SubParsersAction[argparse.ArgumentParser], commands: list[LoadedCLICommand]
    ) -> None:
        """Adds each command as a subcommand - one whose name is taken, or whose arguments fail to
        declare, is reported and skipped.

        Args:
            subparsers: The `hare` subcommand group, with the built-in commands already added.
            commands: The loaded plugin commands.
        """
        sources_by_name: dict[str, str] = dict.fromkeys(subparsers.choices, "hare itself")
        for loaded_command in commands:
            command = loaded_command.command
            if command.name in sources_by_name:
                CLICommandRegistry.warn(
                    f"skipping CLI command {command.name!r} from {loaded_command.source}: the name is "
                    f"already taken by {sources_by_name[command.name]}"
                )
                continue
            # Declared on a detached parser first, so a failing add_arguments() leaves no half-added
            # subcommand behind.
            probe_parser = argparse.ArgumentParser(add_help=False)
            try:
                command.add_arguments(probe_parser)
            except Exception as exc:
                CLICommandRegistry.warn(f"skipping CLI command {command.name!r} from {loaded_command.source}: {exc!r}")
                continue
            sources_by_name[command.name] = loaded_command.source
            command_parser = subparsers.add_parser(command.name, help=command.help)
            command.add_arguments(command_parser)
            command_parser.set_defaults(func=command.run)
