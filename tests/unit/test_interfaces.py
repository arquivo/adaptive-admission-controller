"""Smoke tests proving the ABCs in app.interfaces cannot be instantiated directly.

These exist mainly to fail loudly in later phases if a concrete controller
accidentally forgets to implement one of the abstract methods.
"""

import pytest

from app.interfaces import (
    BackendPolicy,
    CapacityController,
    PenaltyStore,
    Scheduler,
)


@pytest.mark.parametrize("abc_cls", [CapacityController, Scheduler, BackendPolicy, PenaltyStore])
def test_abc_cannot_be_instantiated(abc_cls):
    with pytest.raises(TypeError):
        abc_cls()
