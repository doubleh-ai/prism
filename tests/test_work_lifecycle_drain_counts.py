"""Drain control counters are observable and do not change lifecycle decisions."""

import time

import pytest

from server import work_lifecycle as work


@pytest.fixture(autouse=True)
def temp_data_dir():
    """The lifecycle core never imports application configuration or databases."""
    yield


@pytest.fixture
def manager():
    value = work.Manager(
        work.RuntimeIdentity(
            "12345678-1234-1234-1234-123456789abc",
            90001,
            77,
            "a" * 40,
            "b" * 64,
            "/system.slice/stock-app.service",
        ),
        max_workers=2,
    )
    for kind in work.KINDS:
        value.declare(kind)
    yield value
    # Test cleanup only: never changes blocked leases or returns a drain proof.
    value._pool.shutdown(wait=True, cancel_futures=False)


def observation(manager):
    identity, epoch, generation, nonce = manager.observation_context()
    return work.OSObservation(
        identity,
        epoch,
        generation,
        nonce,
        time.monotonic_ns(),
        "yes",
        ((".", ((identity.pid, identity.start_ticks),)),),
    )


def test_begin_seal_resume_are_counted_in_observe(manager):
    assert manager.observe()["drain_counts"] == {"begin": 0, "seal": 0, "resume": 0}

    manager.begin_drain(manager.ticket())
    snapshot = manager.observe()
    assert snapshot["state"] == "draining"
    assert snapshot["drain_counts"] == {"begin": 1, "seal": 0, "resume": 0}

    manager.record_os_observation(observation(manager))
    manager.seal(manager.ticket())
    snapshot = manager.observe()
    assert snapshot["state"] == "quiescent"
    assert snapshot["drain_counts"] == {"begin": 1, "seal": 1, "resume": 0}

    manager.resume(manager.ticket())
    snapshot = manager.observe()
    assert snapshot["state"] == "running"
    assert snapshot["drain_counts"] == {"begin": 1, "seal": 1, "resume": 1}


def test_rejected_control_attempt_is_counted_without_changing_state(manager):
    manager.begin_drain(manager.ticket())
    with pytest.raises(work.LifecycleError, match="drain_state_rejected"):
        manager.begin_drain(manager.ticket())
    snapshot = manager.observe()
    assert snapshot["state"] == "draining"
    assert snapshot["drain_counts"] == {"begin": 2, "seal": 0, "resume": 0}
