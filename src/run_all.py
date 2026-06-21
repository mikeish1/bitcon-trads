"""
Process supervisor - run one or more bots in a single Railway container.

Each bot (spot trend-follower, funding carry, ETF momentum) is an independent
process with its own loop, state tables, and graceful SIGTERM handling. This
supervisor is PID 1 in the container: it launches the selected bots as children,
forwards SIGTERM/SIGINT to them for a clean shutdown, and restarts a crashed
child with capped backoff so one bot dying never takes the others down.

Selection via the RUN_BOTS env var (comma-separated), default "spot" so existing
deploys are unchanged:

    RUN_BOTS=spot              # default - just the validated trend-follower
    RUN_BOTS=spot,carry,etf    # run all three together

CRITICAL: keep Railway at numReplicas=1. This supervisor runs each bot exactly
once; a second replica would duplicate every bot and place duplicate orders.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from typing import Optional

from loguru import logger

# Bot name -> module run with `python -m <module>`.
BOTS: dict[str, str] = {
    "spot": "src.main_loop",
    "carry": "src.carry.main",
    "etf": "src.etf.main",
}

_GRACE_SECONDS = 15          # how long to wait for children to exit on shutdown
_STABLE_SECONDS = 120        # a child running this long resets its restart backoff
_MAX_BACKOFF = 60


def parse_bots(value: Optional[str]) -> list[str]:
    """Resolve RUN_BOTS into an ordered, de-duplicated list of known bots.
    Unknown names are warned and skipped; an empty result falls back to ["spot"]."""
    out: list[str] = []
    for raw in (value or "spot").split(","):
        name = raw.strip().lower()
        if not name:
            continue
        if name not in BOTS:
            logger.warning("Unknown bot '{}' in RUN_BOTS (known: {}); skipping.",
                           name, ", ".join(BOTS))
            continue
        if name not in out:
            out.append(name)
    return out or ["spot"]


def _command(name: str) -> list[str]:
    return [sys.executable, "-u", "-m", BOTS[name]]


class Supervisor:
    def __init__(self, names: list[str]):
        self.names = names
        self.procs: dict[str, subprocess.Popen] = {}
        self.started_at: dict[str, float] = {}
        self.restarts: dict[str, int] = {n: 0 for n in names}
        self.next_restart: dict[str, float] = {n: 0.0 for n in names}
        self.shutting_down = False

    def _spawn(self, name: str) -> None:
        proc = subprocess.Popen(_command(name))
        self.procs[name] = proc
        self.started_at[name] = time.monotonic()
        logger.info("Started bot '{}' ({}, pid {}).", name, BOTS[name], proc.pid)

    def _handle_signal(self, signum, _frame) -> None:
        if self.shutting_down:
            return
        self.shutting_down = True
        logger.warning("Signal {} received - forwarding SIGTERM to {} child bot(s)...",
                       signum, len(self.procs))
        for name, proc in self.procs.items():
            if proc.poll() is None:
                try:
                    proc.terminate()
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning("Could not signal '{}': {}", name, exc)

    def _drain(self) -> int:
        deadline = time.monotonic() + _GRACE_SECONDS
        while any(p.poll() is None for p in self.procs.values()) and time.monotonic() < deadline:
            time.sleep(0.5)
        for name, proc in self.procs.items():
            if proc.poll() is None:
                logger.warning("Bot '{}' did not exit within {}s - killing.", name, _GRACE_SECONDS)
                proc.kill()
        logger.info("All bots stopped. Supervisor exiting.")
        return 0

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        logger.info("Supervisor up (pid {}). Bots: {}", os.getpid(), ", ".join(self.names))
        for name in self.names:
            self._spawn(name)

        while True:
            if self.shutting_down:
                return self._drain()
            self._reap(time.monotonic())
            time.sleep(2)

    def _reap(self, now: float) -> None:
        """One monitor tick: respawn due children, reset stable backoffs, and
        schedule a backed-off restart for any child that exited unexpectedly."""
        for name in self.names:
            proc = self.procs.get(name)
            if proc is None:                           # awaiting a backoff restart
                if now >= self.next_restart.get(name, 0.0):
                    self._spawn(name)
                continue
            code = proc.poll()
            if code is None:                           # still running
                if self.restarts[name] and now - self.started_at[name] > _STABLE_SECONDS:
                    self.restarts[name] = 0            # ran stably -> reset backoff
                continue
            # Unexpected exit -> schedule a backed-off restart.
            self.restarts[name] += 1
            backoff = min(_MAX_BACKOFF, 5 * self.restarts[name])
            logger.error("Bot '{}' exited (code {}). Restart #{} in {}s.",
                         name, code, self.restarts[name], backoff)
            self.procs.pop(name, None)
            self.next_restart[name] = now + backoff


def main() -> None:
    logger.remove()
    logger.add(sys.stdout, level=os.getenv("LOG_LEVEL", "INFO"),
               format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
                      "<level>SUPERVISOR</level> | <level>{message}</level>")
    names = parse_bots(os.getenv("RUN_BOTS"))
    sys.exit(Supervisor(names).run())


if __name__ == "__main__":
    main()
