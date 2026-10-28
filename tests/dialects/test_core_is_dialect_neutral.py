"""hare's core names no dialect: outside the dialects' own packages (``hare/dialects/`` except
its shared ``base``), no module imports a dialect's package, compares with a dialect's name or
points at a dialect's module, writes SQL only one database has or picks anything by a dialect's name -
everything a dialect decides goes through the ``Dialect``/``Driver`` objects and their registries, so a
new dialect is added without touching hare's core. The syntax several databases write alike beyond ISO SQL
is a class of ``base`` named after what it holds, which the dialect of each such database extends."""

import ast
import io
import re
import tokenize
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.database_independent

HARE_DIRECTORY = Path(__file__).resolve().parents[2] / "hare"
DIALECTS_DIRECTORY = HARE_DIRECTORY / "dialects"
SHARED_DIALECT_DIRECTORY = DIALECTS_DIRECTORY / "base"
BUILTIN_DIALECT_PACKAGES = ("hare.dialects.sqlite", "hare.dialects.postgresql", "hare.dialects.clickhouse")
BUILTIN_DIALECT_NAMES = frozenset({"sqlite", "postgres", "postgresql", "clickhouse"})
BUILTIN_DIALECT_NAME_MEMBERS = frozenset({"SQLITE", "POSTGRESQL", "CLICKHOUSE"})
#: Keywords, catalog names and functions only one of the built-in databases has.
DIALECT_ONLY_SQL = re.compile(
    r"\bCONCURRENTLY\b|\bNOT VALID\b|\bVALIDATE CONSTRAINT\b|\bEXCLUDE\b|\bCREATE EXTENSION\b|"
    r"\bCREATE COLLATION\b|\bSET SCHEMA\b|NULLS NOT DISTINCT|\bINCLUDE \(|\bDISTINCT ON\b|\bILIKE\b|"
    r"\bUSING (?:GIST|GIN|BRIN|SPGIST|HNSW|IVFFLAT)\b|\bpg_[a-z_]+|::[a-z]+\b|\bjsonb\b|\bjsonb_[a-z_]+|"
    r"\bunnest\(|\b(?:BIG|SMALL)?SERIAL\b|\bTABLESPACE\b|\bUNLOGGED\b|\bLISTEN\b|\bNOTIFY\b|\bxmax\b|"
    r"\bNO KEY UPDATE\b|\bto_tsvector\b|\btsquery\b|\bATTACH\b|\bPRAGMA\b|sqlite_(?:master|schema|sequence)|"
    r"\bjson_each\b|\bAUTOINCREMENT\b|WITHOUT ROWID|\bstrftime\b|\bjulianday\b|\bGLOB\b|"
    r"::\{|\$\w*\$|EXECUTE FUNCTION|CONSTRAINT TRIGGER|\bALTER INDEX\b|\bLANGUAGE\b|"
    r"\bDATE_TRUNC\b|\bJSONB?_BUILD_OBJECT\b|\bSTATEMENT_TIMESTAMP\b|\bANY\(|['\)]::|"
    r"(?:^|\s)LIMIT\s*$|\bLIMIT (?:\d|-1)|\bFROM STDIN\b|\bFORMAT Native\b|^BEGIN;?$|"
    r"\bbtree_gist\b|\bbloom\b|\bsearch_path\b|\bON CLUSTER\b|\bKeeper\b|"
    r"\* EXCEPT \(|\bASOF\b|\bARRAY JOIN\b|\bFINAL\b|\bPREWHERE\b|\bLIMIT \S+ BY\b|\btoDate\w*\(|"
    r"\btoString\(|\blengthUTF8\b|\bCOMMIT PREPARED\b|\bPREPARE TRANSACTION\b|\bCOPY\b|\boctet_length\b|"
    r" ->> | @> | <@ | \?& | \?\| "
)
#: The names of the built-in databases, their extensions and drivers - a comment or a docstring of hare's
#: core names what a database does by the feature, not by the database.
DATABASE_NAMES = re.compile(r"\b(?:PostgreSQL|Postgres|SQLite|ClickHouse|SpatiaLite|PgBouncer|pgvector|rust_pg)\b")
#: The catalogs, types, extensions, statements and clauses only one of the built-in databases has - a comment or
#: a docstring of hare's core says what they do instead of writing them.
DATABASE_ONLY_WORDS = re.compile(
    r"\bpg_[a-z_]+|\bjsonb\b|\bhstore\b|\bunaccent\b|\bctid\b|\bcitext\b|\bpostgis\b|\btsvector\b|\bltree\b|"
    r"\bbytea\b|\bsearch_path\b|\bbtree_gist\b|\bbloom\b|\bspgist\b|"
    r"\bCREATE TYPE\b|\b(?:COMMIT|ROLLBACK) PREPARED\b|\bPREPARE TRANSACTION\b|\bLISTEN\b|\bNOTIFY\b|\bCOPY\b|"
    r"\bFROM STDIN\b|\bCONCURRENTLY\b|\bNOT VALID\b|\bVALIDATE CONSTRAINT\b|\bEXCLUDE USING\b|\bSET SCHEMA\b|"
    r"\bNULLS NOT DISTINCT\b|\bDISTINCT ON\b|\bILIKE\b|\bNO KEY UPDATE\b|\bCONSTRAINT TRIGGER\b|\bALTER INDEX\b|"
    r"\bDATE_TRUNC\b|\bunnest\(|\bANY\(|\bPRAGMA\b|\bsqlite_[a-z_]+|\bWITHOUT ROWID\b|"
    r"\bMergeTree\b|\bLowCardinality\b|\bNullable\(|\bASOF\b|\bARRAY JOIN\b|\bPREWHERE\b|\bON CLUSTER\b|"
    r"\bRENAME TABLE\b"
)
#: The modules naming hare's own dialects and drivers, to load them by name.
DATABASE_NAMING_MODULES = frozenset({"hare/dialects/constants.py", "hare/dialects/dialect_registry.py"})
#: The parts of a SQL statement a core module doesn't write itself - outside the SQL builder, SQL text is
#: the dialect's.
CORE_SQL_TEXT = re.compile(r"\bIN \(|\bSELECT\b|\bCAST\(")
#: The events the observers are told of - any dialect may send one, though only one sends it yet.
OBSERVED_EVENTS_MODULE = "hare/instrumentation/declarations.py"
#: An identifier of Python source.
IDENTIFIER_PATTERN = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
#: The modules of ``base`` holding syntax several databases write alike beyond ISO SQL - their SQL
#: isn't one database's.
SEVERAL_DATABASES_SYNTAX_MODULES = frozenset({"hare/dialects/base/clauses/limit_returning_conflict_query_clauses.py"})
#: The name of a function only one of the built-in databases has, given to a ``Function``.
DIALECT_ONLY_FUNCTION_NAMES = frozenset({"ANY", "POW", "NOW", "DATE_TRUNC", "JSONB_BUILD_OBJECT", "JSON_EACH"})
#: The modules a dialect is found by its name in - the dialect itself and the registry of dialects.
DIALECT_NAME_MODULES = frozenset({"hare/dialects/base/dialect.py", "hare/dialects/dialect_registry.py"})
#: The calls taking a key first.
KEYED_CALL_NAMES = frozenset({"get", "pop", "setdefault"})


def get_core_modules(hare_directory: Path = HARE_DIRECTORY) -> Iterator[Path]:
    dialects_directory = hare_directory / "dialects"
    for path in sorted(hare_directory.rglob("*.py")):
        if dialects_directory not in path.parents or dialects_directory / "base" in path.parents:
            yield path


def get_docstring_nodes(tree: ast.AST) -> set[int]:
    docstring_node_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstring_node_ids.add(id(first.value))
    return docstring_node_ids


def names_builtin_dialect(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.lower() in BUILTIN_DIALECT_NAMES
    return isinstance(node, ast.Attribute) and node.attr in BUILTIN_DIALECT_NAME_MEMBERS


def get_last_identifier(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return get_last_identifier(node.func)
    return None


def is_dialect_name(node: ast.AST) -> bool:
    """Whether an expression is a dialect's name: ``dialect.name``, ``connection.dialect.name``,
    ``entry.dialect_name``, a ``dialect_name`` variable."""
    if isinstance(node, ast.Attribute) and node.attr == "dialect_name":
        return True
    if isinstance(node, ast.Name) and (node.id == "dialect_name" or node.id.endswith("_dialect_name")):
        return True
    if isinstance(node, ast.Attribute) and node.attr == "name":
        owner = get_last_identifier(node.value)
        return owner is not None and "dialect" in owner.lower()
    return False


def holds_dialect_name(node: ast.AST) -> bool:
    """Whether an expression is a dialect's name or a tuple holding one - a message isn't looked into."""
    if isinstance(node, ast.Tuple):
        return any(holds_dialect_name(element) for element in node.elts)
    return is_dialect_name(node)


def picks_by_dialect_name(node: ast.AST) -> bool:
    """Whether an expression picks something by a dialect's name - compares with it, reads by it, or makes
    a key of it. A description labelling something with the name picks nothing."""
    if isinstance(node, ast.Compare):
        return any(holds_dialect_name(operand) for operand in (node.left, *node.comparators))
    if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
        return holds_dialect_name(node.slice)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in KEYED_CALL_NAMES:
        return bool(node.args) and holds_dialect_name(node.args[0])
    if isinstance(node, ast.Tuple):
        return any(is_dialect_name(element) for element in node.elts)
    return False


def get_violations(path: Path) -> Iterator[str]:
    return get_violations_of(path, HARE_DIRECTORY.parent)


def get_violations_of(path: Path, root: Path) -> Iterator[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstring_node_ids = get_docstring_nodes(tree)
    location = path.relative_to(root).as_posix()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(BUILTIN_DIALECT_PACKAGES):
            yield f"{location}:{node.lineno} imports {node.module}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(BUILTIN_DIALECT_PACKAGES):
                    yield f"{location}:{node.lineno} imports {alias.name}"
        elif isinstance(node, ast.Compare):
            if any(names_builtin_dialect(operand) for operand in (node.left, *node.comparators)):
                yield f"{location}:{node.lineno} compares with a dialect's name"
        elif isinstance(node, ast.Attribute) and node.attr in BUILTIN_DIALECT_NAME_MEMBERS:
            yield f"{location}:{node.lineno} names a dialect"
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("startswith", "endswith")
            and any(names_builtin_dialect(argument) for argument in node.args)
        ):
            yield f"{location}:{node.lineno} matches a dialect's name"
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstring_node_ids
            and any(package in node.value for package in BUILTIN_DIALECT_PACKAGES)
        ):
            yield f"{location}:{node.lineno} names a dialect's module"
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstring_node_ids
            and location not in SEVERAL_DATABASES_SYNTAX_MODULES
            and (DIALECT_ONLY_SQL.search(node.value) or node.value in DIALECT_ONLY_FUNCTION_NAMES)
        ):
            yield f"{location}:{node.lineno} writes a dialect's SQL"
        if location not in DIALECT_NAME_MODULES and picks_by_dialect_name(node):
            yield f"{location}:{node.lineno} picks by a dialect's name"
        if (
            isinstance(node, ast.Constant)
            and id(node) in docstring_node_ids
            and location not in DATABASE_NAMING_MODULES
            and DATABASE_NAMES.search(node.value)
        ):
            yield f"{location}:{node.lineno} names a database in a docstring"
        if (
            isinstance(node, ast.Constant)
            and id(node) in docstring_node_ids
            and DATABASE_ONLY_WORDS.search(node.value)
        ):
            yield f"{location}:{node.lineno} writes what one database has in a docstring"
        if isinstance(node, ast.JoinedStr) and not writes_sql(location):
            text = "".join(part.value for part in node.values if isinstance(part, ast.Constant))
            if CORE_SQL_TEXT.search(text):
                yield f"{location}:{node.lineno} writes SQL text"
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "replace"
            and [getattr(argument, "value", None) for argument in node.args] == ["'", "''"]
            and not writes_sql(location)
        ):
            yield f"{location}:{node.lineno} escapes a literal by hand"
        if isinstance(node, ast.Compare) and any(
            isinstance(operand, ast.Attribute) and operand.attr == "argument_value_type"
            for operand in (node.left, *node.comparators)
        ):
            yield f"{location}:{node.lineno} tells a field by its value type's message"
    for token in tokenize.generate_tokens(io.StringIO(path.read_text(encoding="utf-8")).readline):
        if token.type != tokenize.COMMENT:
            continue
        if location not in DATABASE_NAMING_MODULES and DATABASE_NAMES.search(token.string):
            yield f"{location}:{token.start[0]} names a database in a comment"
        if DATABASE_ONLY_WORDS.search(token.string):
            yield f"{location}:{token.start[0]} writes what one database has in a comment"


def writes_sql(location: str) -> bool:
    """Whether a module writes SQL text - the SQL builder, or ``base`` of the dialects."""
    return location.startswith(("hare/sql/", "hare/dialects/base/"))


def test_core_names_no_dialect():
    violations = [violation for path in get_core_modules() for violation in get_violations(path)]
    assert violations == []


def test_each_type_of_violation_is_found(tmp_path):
    module = tmp_path / "hare" / "query" / "offending.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        '"""Mentions hare.dialects.sqlite in a docstring, which is fine."""\n'
        "from hare.dialects.sqlite.constants import SQLITE_DIALECT\n"
        "import hare.dialects.postgresql.fields\n"
        "def f(dialect, url):\n"
        "    if dialect.name == 'postgresql' or dialect.name == DialectName.SQLITE:\n"
        "        return url.startswith('sqlite')\n"
        "    if dialect.features.supports_concurrent_indexes:\n"
        "        return 'CREATE INDEX CONCURRENTLY name_idx ON book (name)'\n"
        "    if dialect.features.supports_extensions:\n"
        "        return 'SELECT 1 FROM pg_extension'\n"
        "    if dialect.features.supports_returning:\n"
        "        return Function('DATE_TRUNC', 'day')\n"
        "    if dialect.features.supports_spatial:\n"
        "        return frozenset({DialectName.POSTGRESQL})\n"
        "    if entry.dialect_name != connection.dialect.name:\n"
        "        return implementations.get(query.dialect.name)\n"
        "    if dialect.features.supports_json_type:\n"
        "        return expression[dialect.name], cache[(path, dialect.name)]\n"
        "    raise ValueError(f'no {dialect.name} implementation')\n"
        "    return 'hare.dialects.postgresql.fields.array_field.ArrayField'\n"
        "from hare.dialects.clickhouse.fields import FixedStringField\n"
        "def g(field, text, table):\n"
        '    """Works on PostgreSQL only, reading an hstore."""\n'
        "    # SQLite reads it as text.\n"
        "    # Finished with COMMIT PREPARED.\n"
        "    if field.argument_value_type == INTERVAL_VALUE_TYPE:\n"
        "        return 'SELECT * EXCEPT (hidden) FROM t'\n"
        "    return f'SELECT a FROM {table}', text.replace(\"'\", \"''\")\n",
        encoding="utf-8",
    )
    violations = sorted(violation.split(" ", 1)[1] for violation in get_violations_of(module, tmp_path))
    assert violations == sorted(
        [
            "imports hare.dialects.sqlite.constants",
            "imports hare.dialects.postgresql.fields",
            "compares with a dialect's name",
            "compares with a dialect's name",
            "matches a dialect's name",
            "names a dialect",
            "names a dialect",
            "names a dialect's module",
            "writes a dialect's SQL",
            "writes a dialect's SQL",
            "writes a dialect's SQL",
            # dialect.name == ... twice, entry.dialect_name != ..., .get(...), expression[...], cache[(...)]
            # and the (path, dialect.name) key itself - the message isn't one.
            *["picks by a dialect's name"] * 7,
            "imports hare.dialects.clickhouse.fields",
            "names a database in a docstring",
            "names a database in a comment",
            "writes what one database has in a docstring",
            "writes what one database has in a comment",
            "tells a field by its value type's message",
            "writes a dialect's SQL",
            "writes SQL text",
            "escapes a literal by hand",
        ]
    )
    assert list(get_core_modules())


def test_no_module_outside_the_clickhouse_dialect_names_it():
    """The ClickHouse dialect reaches hare only through its hooks and ``Features`` - no other module of
    hare names it but the list of hare's own dialects and drivers, which imports it by its name."""
    clickhouse_directory = DIALECTS_DIRECTORY / "clickhouse"
    naming_modules = [
        path.relative_to(HARE_DIRECTORY).as_posix()
        for path in sorted(HARE_DIRECTORY.rglob("*.py"))
        if clickhouse_directory not in path.parents and "clickhouse" in path.read_text(encoding="utf-8").lower()
    ]
    assert naming_modules == ["dialects/constants.py"]


def get_dialect_package(path: Path, hare_directory: Path) -> str | None:
    """The built-in dialect package a module belongs to, None for a core module."""
    for package in BUILTIN_DIALECT_PACKAGES:
        if (hare_directory.parent / package.replace(".", "/")) in path.parents:
            return package
    return None


def get_feature_names_no_core_module_reads(hare_directory: Path) -> list[str]:
    """The fields of ``Features`` read by no module of hare's core - by one dialect's alone."""
    features_path = hare_directory / "dialects" / "base" / "features.py"
    features_tree = ast.parse(features_path.read_text(encoding="utf-8"))
    [features_class] = [
        node for node in ast.walk(features_tree) if isinstance(node, ast.ClassDef) and node.name == "Features"
    ]
    field_names = [
        statement.target.id
        for statement in features_class.body
        if isinstance(statement, ast.AnnAssign)
        and isinstance(statement.target, ast.Name)
        and "ClassVar" not in ast.unparse(statement.annotation)
    ]
    core_words: set[str] = set()
    for path in get_core_modules(hare_directory):
        if path != features_path:
            core_words.update(IDENTIFIER_PATTERN.findall(path.read_text(encoding="utf-8")))
    return [name for name in field_names if name not in core_words]


def get_core_names_serving_one_dialect(hare_directory: Path) -> list[str]:
    """The constants and classes of hare's core that only one dialect's package uses."""
    paths_by_word: dict[str, set[Path]] = {}
    for path in sorted(hare_directory.rglob("*.py")):
        for word in set(IDENTIFIER_PATTERN.findall(path.read_text(encoding="utf-8"))):
            paths_by_word.setdefault(word, set()).add(path)
    serving_one_dialect = []
    for path in get_core_modules(hare_directory):
        if path.relative_to(hare_directory.parent).as_posix() == OBSERVED_EVENTS_MODULE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        defined_names = [
            target.id
            for statement in tree.body
            if isinstance(statement, ast.Assign)
            for target in statement.targets
            if isinstance(target, ast.Name) and target.id.isupper()
        ] + [statement.name for statement in tree.body if isinstance(statement, ast.ClassDef)]
        for name in defined_names:
            using_paths = paths_by_word.get(name, set()) - {path}
            using_packages = {get_dialect_package(other, hare_directory) for other in using_paths}
            if using_paths and len(using_packages) == 1 and None not in using_packages:
                serving_one_dialect.append(f"{path.relative_to(hare_directory.parent).as_posix()}: {name}")
    return serving_one_dialect


def test_each_feature_of_every_dialect_is_read_outside_the_dialects():
    """A field of ``Features`` only one dialect reads belongs to that dialect's subclass of it."""
    assert get_feature_names_no_core_module_reads(HARE_DIRECTORY) == []


def test_no_name_of_the_core_serves_one_dialect_alone():
    """A constant or a class of hare's core that only one dialect's package uses belongs to that package."""
    assert get_core_names_serving_one_dialect(HARE_DIRECTORY) == []


def test_a_feature_and_a_name_serving_one_dialect_are_found(tmp_path):
    hare_directory = tmp_path / "hare"
    for relative_path, text in {
        "dialects/base/features.py": "class Features:\n    shared_flag: bool = False\n    sqlite_flag: bool = False\n",
        "query/reader.py": (
            "SHARED_LIMIT = 1\nONE_DIALECT_LIMIT = 2\ndef f(features):\n    return features.shared_flag\n"
        ),
        "dialects/sqlite/user.py": (
            "def g(features):\n    return features.sqlite_flag, SHARED_LIMIT, ONE_DIALECT_LIMIT\n"
        ),
        "dialects/postgresql/user.py": "LIMIT = SHARED_LIMIT\n",
    }.items():
        module = hare_directory / relative_path
        module.parent.mkdir(parents=True, exist_ok=True)
        module.write_text(text, encoding="utf-8")

    assert get_feature_names_no_core_module_reads(hare_directory) == ["sqlite_flag"]
    assert get_core_names_serving_one_dialect(hare_directory) == ["hare/query/reader.py: ONE_DIALECT_LIMIT"]
