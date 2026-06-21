"""Tests for the multi-bot process supervisor (selection + command wiring)."""
from __future__ import annotations

import importlib.util

from src.run_all import BOTS, _command, parse_bots


def test_default_is_spot_only():
    assert parse_bots(None) == ["spot"]
    assert parse_bots("") == ["spot"]
    assert parse_bots("   ") == ["spot"]


def test_run_all_three():
    assert parse_bots("spot,carry,etf") == ["spot", "carry", "etf"]


def test_dedupe_and_strip_and_case():
    assert parse_bots(" ETF , etf ,Spot") == ["etf", "spot"]


def test_unknown_names_skipped_then_default():
    assert parse_bots("bogus") == ["spot"]          # nothing valid -> default
    assert parse_bots("carry,bogus") == ["carry"]   # keep the valid one


def test_command_targets_the_right_module():
    assert _command("carry")[-2:] == ["-m", "src.carry.main"]
    assert _command("etf")[1] == "-u"               # unbuffered child output


def test_every_bot_module_is_importable():
    for module in BOTS.values():
        assert importlib.util.find_spec(module) is not None


class _FakeProc:
    """Minimal Popen stand-in: poll() returns None until `exit_code` is set."""
    def __init__(self):
        self.exit_code = None

    def poll(self):
        return self.exit_code


def test_reap_restarts_crashed_child_with_backoff():
    from src.run_all import Supervisor

    sup = Supervisor(["spot"])
    spawned = []
    sup._spawn = lambda name: (spawned.append(name),                       # record
                               sup.procs.__setitem__(name, _FakeProc()),
                               sup.started_at.__setitem__(name, 0.0))

    sup._spawn("spot")                       # initial start
    assert spawned == ["spot"]

    # Child still running -> reap is a no-op.
    sup._reap(now=1.0)
    assert sup.restarts["spot"] == 0 and "spot" in sup.procs

    # Child crashes -> reap schedules a backed-off restart (5s), not immediate.
    sup.procs["spot"].exit_code = 1
    sup._reap(now=10.0)
    assert sup.restarts["spot"] == 1
    assert "spot" not in sup.procs                       # removed, awaiting restart
    assert sup.next_restart["spot"] == 15.0              # 10 + 5*1 backoff

    # Before the backoff elapses, reap does NOT respawn.
    sup._reap(now=12.0)
    assert spawned == ["spot"]                           # still just the first start
    # After it elapses, reap respawns.
    sup._reap(now=15.0)
    assert spawned == ["spot", "spot"]


def test_reap_resets_backoff_after_stable_run():
    from src.run_all import Supervisor, _STABLE_SECONDS

    sup = Supervisor(["carry"])
    sup.restarts["carry"] = 3
    sup.procs["carry"] = _FakeProc()                     # running (poll -> None)
    sup.started_at["carry"] = 0.0
    sup._reap(now=_STABLE_SECONDS + 1)                   # ran longer than the stable window
    assert sup.restarts["carry"] == 0
