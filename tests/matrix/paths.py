from pathlib import Path

REPOSITORY_DIRECTORY = Path(__file__).resolve().parents[2]
TESTS_DIRECTORY = REPOSITORY_DIRECTORY / "tests"
MATRIX_LOG_DIRECTORY = REPOSITORY_DIRECTORY / ".test-matrix"
MATRIX_CATALOG_PATH = Path(__file__).with_name("catalog.toml")
