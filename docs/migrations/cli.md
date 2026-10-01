# The `hare` command

The `hare` command (installed as a console script) reads its config from `-c/--config` - either
`module.VARIABLE` (a dotted path to a config value, e.g. `settings.HARE_ORM`) or the path of a
`.json`/`.yml`/`.yaml` file - and without it from the `HARE_ORM` environment variable or
`[tool.hare].hare_orm` in `pyproject.toml`, which hold the same type of value. It is what
[`HareConfig.load()`](../connections/configuration.md#the-config-dict) takes.

| Command | Purpose |
|---|---|
| `hare init [app_labels...]` | Create a migrations package for the given (or all) apps. |
| `hare shell` | IPython shell with `Hare`, `hare` (the `HareContext`), `apps`, and every model already in scope. |
| `hare makemigrations [app_labels...] [--empty] [--merge] [--check] [--dry-run] [-n NAME]` | Generate migrations by diffing model state. `--empty` needs an app label and creates a blank migration depending on the current heads. `--merge` needs an app label and writes one migration joining the app's forked history (two heads); without it a forked history is an error. `--dry-run` prints what would be written without writing; `--check` does the same and exits `1` when there is anything to write (neither combines with `--empty`). `-n` renames every migration generated in the run, dependencies between them included. Without an explicit `migrations` module, an app whose models live in a top-level `models.py` (no package) gets a top-level `migrations` package next to it. Two apps that would end up with the same migrations package (two such single-file apps in one directory, or the same explicit `migrations` value) are refused with an error by every migration command - set a distinct `migrations` module for each (e.g. `"migrations_billing"`). |
| `hare squashmigrations APP_LABEL [-n NAME]` | Collapse **all** of an app's migrations into one. `replaces=` is written for documentation only — the loader/executor don't read it, so this is only safe on a fresh/never-applied database. |
| `hare migrate [app_label] [migration] [--fake] [--dry-run]` | Apply migrations forward or backward, whichever the current DB state calls for: a `migration` after the applied ones applies up to it, one before them unapplies back to it, and `zero` unapplies every migration of the app (`hare migrate models zero`). Without `migration` the app goes to its latest; without `app_label` every app does. `--fake` records history without running SQL; `--dry-run` prints the plan only and writes nothing to the database, not even the migrations table. |
| `hare history [app_labels...]` | List migrations already applied (from the DB), grouped by connection/app. |
| `hare heads [app_labels...]` | List head migrations on disk (not from the DB). |
| `hare inspectdb [tables...] [--connection ALIAS] [--schema SCHEMA]` | Reverse-engineer hare-orm models from an existing schema (like Django's `inspectdb`). `--schema` defaults to the connection's default schema (Postgres `current_schema()`); a table outside it gets `Meta.schema`. |
| `hare sqlmigrate APP_LABEL MIGRATION_NAME [--backward]` | Print a migration's SQL without running it (wrapped in `BEGIN;`/`COMMIT;` for Postgres-family engines). `--backward` prints the rollback SQL. |
| `hare drift app_labels... [--connection ALIAS] [--schema SCHEMA]` | Compare the live database against current model state — tables/columns present in the database but not in any migration, and operations that would be needed to reconcile the rest. Exits `1` if anything is found. `Meta.managed = False` models are skipped entirely, same as `makemigrations`. `--schema` picks the schema swept for untracked tables (default: the connection's current schema). hare-orm's own bookkeeping tables (`hare_migrations`, `Transactions.distributed()`'s `hare_distributed_decisions`) are never reported as untracked. |
| `hare distributed-recover --coordinator ALIAS [--finish] [--older-than SECONDS]` | Report (or, with `--finish`, actually finish) `Transactions.distributed()` prepared transactions that never resolved. `--coordinator` (required) is the connection alias holding the `hare_distributed_decisions` decision log. `--finish` issues `COMMIT PREPARED`/`ROLLBACK PREPARED` instead of only reporting. `--older-than` ignores anything younger than that many seconds, since it may still be mid-flight (default: 300); `0` ignores nothing. Exits `1` if any stale prepared transaction was found (report mode) or left unresolved after `--finish`. |

```bash
hare -c settings.HARE_ORM makemigrations models -n add_user_email
hare -c settings.HARE_ORM migrate models 0003_add_index --dry-run
hare -c settings.HARE_ORM migrate models 0002_add_user_email   # back to 0002: unapplies 0003
hare -c settings.HARE_ORM migrate models zero                  # unapplies every migration of the app
hare -c settings.HARE_ORM sqlmigrate models 0003_add_index
hare -c settings.HARE_ORM inspectdb --connection default --schema public users orders
```

Exit codes: `0` success, `2` usage error (`CLIUsageError`, printed to stderr), `1` any other
failure (`CLIError`).

## Adding your own commands {: #adding-your-own-commands }

Installed packages and your project can add subcommands to `hare` without touching hare-orm.
A command is a `CLICommand` subclass:

```python
# myproject/cli.py
from hare.cli.plugins import CLICommand, CLIError, CommandContext


class ExportCommand(CLICommand):
    name = "export"                   # `hare export`
    help = "Export orders to CSV."    # shown in `hare --help`

    def add_arguments(self, parser):
        parser.add_argument("--since")

    async def run(self, ctx, args):
        config = CommandContext.load_config(ctx)        # the same -c/--config config
        async with CommandContext.hare_cli_context(config):
            ...                                         # models and connections are ready here
        return 0                                        # exit code; None also means 0
```

`ctx` is a `CLIContext` (`hare.cli.plugins`) holding the global option `config` - the
`-c`/`--config` value (`module.VARIABLE` or a file path), `None` when not given: then
`CommandContext.load_config(ctx)` locates the config through the environment or
`pyproject.toml`, like every built-in command.

`run()` raising `CLIUsageError` exits with code 2, `CLIError` with code 1, the message printed
to stderr - the same as built-in commands. Any other hare-orm exception (`ConfigurationError`,
`DBConnectionError`, ...) also exits with code 1 and prints its type and message instead of a
traceback; so does a built-in command whose configuration can't be loaded (an unknown engine, a
bad port, ...). The module defining a command is imported on every
`hare` run, so keep it light and import heavy dependencies inside `run()`.

Two ways to register a command:

- **An installed package** declares an entry point in the `hare.cli` group of its own
  `pyproject.toml`; the command appears once the package is installed:

  ```toml
  [project.entry-points."hare.cli"]
  export = "myproject.cli:ExportCommand"
  ```

- **The project itself** lists commands in its config, next to `connections` and `apps`
  (`Hare.init()` ignores this section - only the CLI reads it):

  ```python
  HARE_ORM = {
      "connections": {...},
      "apps": {...},
      "cli": {"commands": ["myproject.cli:ExportCommand"]},
  }
  ```

A command whose name is already taken - by a built-in command or an earlier plugin - is skipped
with a warning naming both sources; built-in commands always win. A plugin that fails to import
or to declare its arguments is skipped with a warning too, and every other command keeps
working.
