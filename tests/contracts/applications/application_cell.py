from __future__ import annotations

import dataclasses
from pathlib import Path

from tests.contracts.constants import APPLICATION_SOURCES_DIRECTORY, APPLICATION_TEST_NAMES


@dataclasses.dataclass(frozen=True)
class ApplicationCell:
    """One application of an integration, run by one kind of test client, under one of the plugin's fixtures.

    Attributes:
        integration: The integration - the test's id with the client.
        client: How the test client runs the application: in the test's event loop or in a thread of its own.
        source_name: The file of the application and the function writing a note through it.
        fixture: The fixture of the plugin the tests take.
        requires: The extras the application needs.
    """

    integration: str
    client: str
    source_name: str
    fixture: str
    requires: tuple[str, ...]

    def is_refused(self, connections_bound_to_event_loop: bool) -> bool:
        """Whether the application's writes are refused - under ``hare_app_db`` they go into the test's
        transaction, which a driver whose connections belong to the test's event loop runs on that loop only.

        Args:
            connections_bound_to_event_loop: Whether the database's driver binds its connections to a loop.

        Returns:
            True for an application in a loop of its own under ``hare_app_db`` on such a driver.
        """
        return connections_bound_to_event_loop and self.fixture == "hare_app_db" and self.client != "the tests loop"

    def get_expected_outcomes(self, connections_bound_to_event_loop: bool) -> dict[str, str]:
        """The outcome of each test of the cell.

        Args:
            connections_bound_to_event_loop: Whether the database's driver binds its connections to a loop.

        Returns:
            Each test's name and outcome, as ``pytest -v`` prints it.
        """
        if not self.is_refused(connections_bound_to_event_loop):
            return dict.fromkeys(APPLICATION_TEST_NAMES, "PASSED")
        # The write through the application and the check after it fail; the next test sees no row.
        write_test, rollback_test, stop_test = APPLICATION_TEST_NAMES
        return {write_test: "FAILED", rollback_test: "PASSED", stop_test: "FAILED"}

    def get_module_name(self) -> str:
        """The name of the cell's test module in the pytest run of its integration.

        Returns:
            The name.
        """
        return f"test_{self.source_name}_{self.fixture}"

    def get_source(self) -> str:
        """The test module of the cell - the application and the tests of the fixture.

        Returns:
            The source.
        """
        directory = Path(APPLICATION_SOURCES_DIRECTORY)
        application = (directory / f"{self.source_name}.txt").read_text(encoding="utf-8")
        tests = (directory / "tests.txt").read_text(encoding="utf-8").replace("{fixture}", self.fixture)
        return f"{application}\n\n{tests}"
