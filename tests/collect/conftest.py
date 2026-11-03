from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from rebalance.collect import Settings

TEST_KEY = "TESTKEY123"


@pytest.fixture
def write_env(tmp_path: Path) -> Callable[..., Path]:
    def _write(content: str, folder: Path | None = None) -> Path:
        path = (folder or tmp_path) / ".env"
        path.write_text(content, encoding="utf-8")
        return path

    return _write


@pytest.fixture
def make_settings(tmp_path: Path) -> Callable[..., Settings]:
    def _make(**overrides: Any) -> Settings:
        base = Settings(api_key=TEST_KEY, data_dir=tmp_path / "data")
        return replace(base, **overrides)

    return _make
