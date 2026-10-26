"""The actual vendor metadata and lock select one JavaScript namespace."""

import importlib.metadata
from pathlib import Path

import pytest
from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

ROOT = Path(__file__).resolve().parent.parent
ENGINES = {"mini-racer", "py-mini-racer"}


def selected_pins(platform_system):
    environment = {**default_environment(), "platform_system": platform_system}
    text = (ROOT / "requirements.lock").read_text().replace("\\\n", " ")
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        requirement = Requirement(line.split(" --hash=", 1)[0].strip())
        if requirement.marker is None or requirement.marker.evaluate(environment):
            result[canonicalize_name(requirement.name)] = requirement
    return result, environment


@pytest.mark.parametrize("system,expected", [("Linux", "py-mini-racer"), ("Darwin", "mini-racer")])
def test_real_akshare_declarations_match_the_single_locked_engine(system, expected):
    pins, environment = selected_pins(system)
    assert set(pins) & ENGINES == {expected}
    declarations = importlib.metadata.requires("akshare")
    assert declarations, "Validate against installed vendor metadata, not a prompt or guessed dependency"
    active = []
    for text in declarations:
        requirement = Requirement(text)
        name = canonicalize_name(requirement.name)
        if name in ENGINES and (requirement.marker is None or requirement.marker.evaluate(environment)):
            active.append(name)
            locked = next(iter(pins[name].specifier)).version
            assert Version(locked) in requirement.specifier
    assert active == [expected]
