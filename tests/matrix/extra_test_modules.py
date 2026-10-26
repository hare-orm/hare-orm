from __future__ import annotations

import ast
from pathlib import Path
from typing import ClassVar

from tests.matrix.constants import (
    MATRIX_PACKAGE,
    NATIVE_REQUIREMENT,
    REPOSITORY_DIRECTORY,
    SUITE_ENVIRONMENTS,
    TESTS_DIRECTORY,
)
from tests.matrix.suite_environment import SuiteEnvironment
from tests.matrix.suite_requirements import SuiteRequirements

#: A module-level name of a module of the test suite - the module's file and the name; None for the whole module.
ModuleName = tuple[Path, str | None]


class ExtraTestModules:
    """The test modules a run of an environment of extras collects instead of the whole suite - pytest
    collecting every module took most of such a run. A test of an environment requires an extra or group of
    it, and every requirement is written as a string somewhere: in ``REQUIRES`` of its module or of a
    ``conftest.py`` above it, in its ``requires`` mark, or in what it imports from a module of the test suite
    (the samples a mark is made from). So a module is collected when it, a ``conftest.py`` above it, or the
    definition of a name it imports - with the names that definition reads, at any depth - names an extra or
    group installing nothing the environment doesn't; one collected needlessly is left out by the
    environment's selection as before."""

    #: The extra and group names a module names anywhere in it, by module file.
    module_names: ClassVar[dict[Path, frozenset[str]]] = {}
    #: The extra and group names the definition of each module-level name names, by module file.
    definition_names: ClassVar[dict[Path, dict[str, frozenset[str]]]] = {}
    #: The names the definition of each module-level name reads - of its module or imported - by module file.
    definition_references: ClassVar[dict[Path, dict[str, frozenset[ModuleName]]]] = {}
    #: What a module imports from the test suite as a whole module, by module file.
    whole_module_imports: ClassVar[dict[Path, frozenset[ModuleName]]] = {}

    @staticmethod
    def get_requirement_names() -> frozenset[str]:
        """Every extra and group an environment installs.

        Returns:
            The names.
        """
        return frozenset(
            name for environment in SUITE_ENVIRONMENTS for name in (*environment.extras, *environment.groups)
        )

    @classmethod
    def get_test_paths(cls, environment: SuiteEnvironment) -> tuple[str, ...]:
        """The test modules that may hold tests of an environment.

        Args:
            environment: The environment - one of extras.

        Returns:
            Their paths relative to the repository, empty when no module may - none whose ``REQUIRES`` (its own,
            a ``conftest.py``'s) the environment doesn't install: pytest stops at a path given that it leaves out.
        """
        installed = SuiteRequirements.get_installed(SuiteRequirements.get_requirements(environment))
        environment_names = {
            name
            for name in cls.get_requirement_names()
            if SuiteRequirements.get_installed(frozenset({name})) <= installed
        }
        return tuple(
            path.relative_to(REPOSITORY_DIRECTORY).as_posix()
            for path in sorted(TESTS_DIRECTORY.rglob("test_*.py"))
            if cls.get_reached_names(path) & environment_names
            and SuiteRequirements.get_installed(SuiteRequirements.get_path_requirements(path) - {NATIVE_REQUIREMENT})
            <= installed
        )

    @classmethod
    def get_reached_names(cls, path: Path) -> set[str]:
        """The names a test module and the ``conftest.py`` files above it name, and those of the definitions
        of what they import.

        Args:
            path: The test module.

        Returns:
            The names.
        """
        pending: list[ModuleName] = [(path, None)]
        directory = path.parent
        while directory.is_relative_to(TESTS_DIRECTORY):
            conftest_path = directory / "conftest.py"
            if conftest_path.is_file():
                pending.append((conftest_path, None))
            directory = directory.parent
        seen: set[ModuleName] = set()
        names: set[str] = set()
        while pending:
            module_name = pending.pop()
            if module_name in seen:
                continue
            seen.add(module_name)
            module_path, name = module_name
            cls.read_module(module_path)
            if name is None:
                names |= cls.module_names[module_path]
                pending.extend(cls.whole_module_imports[module_path])
                for references in cls.definition_references[module_path].values():
                    pending.extend(references)
                continue
            names |= cls.definition_names[module_path].get(name, frozenset())
            pending.extend(cls.definition_references[module_path].get(name, frozenset()))
        return names

    @classmethod
    def read_module(cls, path: Path) -> None:
        """Reads the names of a module and of the definition of each of its module-level names, once.

        Args:
            path: The module's file.
        """
        if path in cls.module_names:
            return
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported: dict[str, ModuleName] = {}
        whole_modules: set[ModuleName] = set()
        defined: dict[str, list[ast.AST]] = {}
        for statement in tree.body:
            if isinstance(statement, ast.ImportFrom) and statement.module is not None and statement.level == 0:
                for alias in statement.names:
                    submodule_path = cls.get_module_file(f"{statement.module}.{alias.name}")
                    if submodule_path is not None:
                        imported[alias.asname or alias.name] = (submodule_path, None)
                        continue
                    module_path = cls.get_module_file(statement.module)
                    if module_path is not None:
                        imported[alias.asname or alias.name] = (module_path, alias.name)
            elif isinstance(statement, ast.Import):
                for alias in statement.names:
                    module_path = cls.get_module_file(alias.name)
                    if module_path is not None:
                        whole_modules.add((module_path, None))
            for target_name in cls.get_bound_names(statement):
                defined.setdefault(target_name, []).append(statement)
        cls.module_names[path] = frozenset(cls.get_named_requirements(tree))
        cls.whole_module_imports[path] = frozenset(whole_modules)
        cls.definition_names[path] = {
            name: frozenset(name for statement in statements for name in cls.get_named_requirements(statement))
            for name, statements in defined.items()
        }
        cls.definition_references[path] = {
            name: frozenset(
                reference
                for statement in statements
                for node in ast.walk(statement)
                if isinstance(node, ast.Name)
                for reference in cls.get_reference(path, node.id, defined, imported)
            )
            for name, statements in defined.items()
        }
        # A module-level statement binding no name (a call, a loop) reads what it reads for every test.
        for statement in tree.body:
            if not cls.get_bound_names(statement):
                for node in ast.walk(statement):
                    if isinstance(node, ast.Name):
                        cls.whole_module_imports[path] |= cls.get_reference(path, node.id, defined, imported)

    @staticmethod
    def get_reference(
        path: Path, name: str, defined: dict[str, list[ast.AST]], imported: dict[str, ModuleName]
    ) -> frozenset[ModuleName]:
        """What a name read in a module stands for.

        Args:
            path: The module's file.
            name: The name read.
            defined: The module's own module-level names.
            imported: The names it imports from the test suite.

        Returns:
            The module-level name - of the module or imported - none for another name.
        """
        if name in imported:
            return frozenset({imported[name]})
        if name in defined:
            return frozenset({(path, name)})
        return frozenset()

    @staticmethod
    def get_bound_names(statement: ast.stmt) -> list[str]:
        """The module-level names a statement defines - by assignment, a function or a class.

        Args:
            statement: A statement of a module's body.

        Returns:
            The names.
        """
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return [statement.name]
        targets: list[ast.expr] = []
        if isinstance(statement, ast.Assign):
            targets = statement.targets
        elif isinstance(statement, (ast.AnnAssign, ast.AugAssign)):
            targets = [statement.target]
        return [node.id for target in targets for node in ast.walk(target) if isinstance(node, ast.Name)]

    @classmethod
    def get_named_requirements(cls, tree: ast.AST) -> set[str]:
        """The extra and group names a piece of code names in strings.

        Args:
            tree: The code.

        Returns:
            The names.
        """
        requirement_names = cls.get_requirement_names()
        return {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in requirement_names
        }

    @staticmethod
    def get_module_file(module_name: str) -> Path | None:
        """The file of a module of the test suite - its package's ``__init__.py`` for a package.

        Args:
            module_name: The dotted name.

        Returns:
            The file, None for a module outside the test suite, of its matrix - which declares every
            environment, the requirement of no test - or a name that is no module.
        """
        if module_name != "tests" and not module_name.startswith("tests."):
            return None
        if module_name == MATRIX_PACKAGE or module_name.startswith(f"{MATRIX_PACKAGE}."):
            return None
        module_path = REPOSITORY_DIRECTORY.joinpath(*module_name.split("."))
        for candidate in (module_path.with_suffix(".py"), module_path / "__init__.py"):
            if candidate.is_file():
                return candidate
        return None
