from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from videre.config import Settings
from videre.main import create_app
from videre.storage.db import Store


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--live", action="store_true", help="run tests that hit the PayPal sandbox or Gemini"
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--live"):
        return
    skip_live = pytest.mark.skip(reason="live test: pass --live to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings that ignore the developer's .env and use a throwaway database."""
    return Settings(_env_file=None, rail="mock", db_path=tmp_path / "test.db")


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    s = Store(tmp_path / "store.db")
    await s.open()
    yield s
    await s.close()


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as c:
        yield c
