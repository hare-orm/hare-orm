from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class SuiteEnvironment:
    """An environment the tests run in: the extras and poetry groups its virtualenv installs - nothing
    else optional is - and whether hare's ``rust.native`` extension is there. A test runs in the
    environments whose extras and groups are what it requires (``REQUIRES`` of its module,
    ``@pytest.mark.requires(...)`` of the test) - a test requiring nothing in ``core`` - and, unless it
    requires ``native``, in the one of the same packages without the extension too (``pure``); the ``all``
    environment installs every extra and group and runs every test.

    Attributes:
        name: The environment's name - pytest's ``--hare-suite-environment``.
        extras: The extras installed.
        groups: The poetry groups installed besides ``main`` and ``test``.
        databases: The databases its suites run on - ``sqlite`` or a key of ``MATRIX_DATABASE_DRIVER_EXTRAS``;
            the extra of a database's driver joins the virtualenv of a run on it, the tests chosen stay the
            environment's. Empty for an environment of extras: its tests run on the databases in the ``all``
            environment.
        native: Whether ``rust.native`` is installed - the pure wheel has none.
        whole_suite: Whether its runs take the whole suite, or most of it - run with pytest-xdist's workers,
            one at a time; the runs of an environment of extras, a few tests each, in one process, several at
            once.
        on_newest_servers_only: Whether its database suites run on the newest version of each server and the
            newest Python only - the checks of hare without its optional packages, or without
            ``rust.native``, on a database.
        database_independent: Whether it runs the tests reading no database.
    """

    name: str
    extras: tuple[str, ...] = ()
    groups: tuple[str, ...] = ()
    databases: tuple[str, ...] = ()
    native: bool = True
    whole_suite: bool = False
    on_newest_servers_only: bool = False
    database_independent: bool = True
