from collections.abc import Callable
from pathlib import Path

import pytest

from pedal_balance.fitfile import Fit

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures() -> Path:
    """Folder with the anonymized real rides (see tests/anonymize.py)."""
    return FIXTURES


@pytest.fixture
def load_fit() -> Callable[[str], Fit]:
    return lambda name: Fit.read(FIXTURES / name)
