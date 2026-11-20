from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class SuiteEnvironment:
    """An environment the tests run in: the extras and poetry groups its virtualenv installs - nothing
    else optional is. A test runs in the environment whose extras and groups are what it requires (the
    module's manifest requirements, ``@pytest.mark.requires(...)`` of the test) - a test requiring
    nothing in ``core``; the ``all`` environment installs compatible extras and groups and runs their tests.

    Attributes:
        name: The environment's name - pytest's ``--hare-suite-environment``.
        extras: The extras installed.
        groups: The poetry groups installed besides ``main`` and ``test``.
        databases: The databases its suites run on - ``sqlite`` or a key of ``MATRIX_DATABASE_DRIVER_EXTRAS``;
            the extra of a database's driver joins the virtualenv of a run on it, the tests chosen stay the
            environment's. Empty for an environment of extras: its tests run on the databases in the ``all``
            environment.
        whole_suite: Whether its runs take the whole suite, or most of it - run with pytest-xdist's workers,
            rather than a small extras-specific selection in one process. RunScheduler controls concurrency
            separately through job limits and shared resource claims.
        on_newest_servers_only: Whether its database suites run on the newest version of each server and the
            newest compatible Python per database driver - checks of hare without optional packages.
        database_independent: Whether it runs the tests reading no database.
    """

    name: str
    extras: tuple[str, ...] = ()
    groups: tuple[str, ...] = ()
    databases: tuple[str, ...] = ()
    whole_suite: bool = False
    on_newest_servers_only: bool = False
    database_independent: bool = True
