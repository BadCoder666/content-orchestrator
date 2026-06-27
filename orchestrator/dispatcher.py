"""
dispatcher.py — the single entrypoint launchd wakes every ~12 minutes.

It does NOT schedule with sleeps; it is stateless per invocation: compute the
local time, ask each Job if it is due, run the due ones, record once-per-slot
jobs in the ledger, log a line. This is the one process that replaces every
separate plist (incl. the Sunday Token-Usage job).

CLI:
  python -m orchestrator.dispatcher              # normal heartbeat tick
  python -m orchestrator.dispatcher --dry-run    # no Slack/Claude/Chrome/subprocess
  python -m orchestrator.dispatcher --job NAME   # force-run one job now
  python -m orchestrator.dispatcher --list       # show the manifest
"""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, time
from pathlib import Path

from . import config
from .jobs import JOBS
from .schedule import Ledger, now_local

log = logging.getLogger("orchestrator")

# The two scheduled dark-wake windows (IST). The Mac is woken (display OFF) for
# these; outside them the dispatcher must never touch the display so daytime use
# is unaffected.
WINDOW_A = (time(0, 45), time(2, 0))    # overnight batch (matches the wake-armer hold)
WINDOW_B = (time(6, 0), time(8, 30))    # approval polling


def _in_dark_window(now) -> bool:
    t = now.time()
    return WINDOW_A[0] <= t <= WINDOW_A[1] or WINDOW_B[0] <= t <= WINDOW_B[1]


def _display_off() -> None:
    """Force the screen off on a scheduled wake (keeps it dark at 01:00/06:00).
    No root needed; best-effort."""
    try:
        subprocess.run(["/usr/bin/pmset", "displaysleepnow"], timeout=10,
                       capture_output=True)
    except Exception:
        pass


def _ensure_network() -> None:
    """Before any dark-window work, make sure we have internet — bring Wi-Fi up
    and fall back to the phone hotspot ("My Phone") if the primary network is
    unreachable. Best-effort and quiet when already online. The root wake-armer
    also runs this at 00:46; doing it on every dark-window tick covers Window B
    (no wake-armer there) and any mid-window Wi-Fi drop."""
    script = config.HERE / "wake" / "ensure_network.sh"
    try:
        r = subprocess.run([str(script), config.WIFI_IFACE, *config.HOTSPOT_SSIDS],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            log.warning("ensure_network could not get online: %s",
                        (r.stdout or "").strip().splitlines()[-1:] or "?")
    except Exception as exc:
        log.warning("ensure_network failed to run: %s", exc)


def notify_local(message: str) -> None:
    """Out-of-band alert that does NOT depend on internet/Slack — a macOS
    notification, so a TOTAL overnight outage (no Wi-Fi, Slack unreachable) still
    surfaces to the user when he's back at the Mac. Best-effort."""
    try:
        subprocess.run(
            ["/usr/bin/osascript", "-e",
             f'display notification {json.dumps(message)} with title "Claude orchestrator"'],
            timeout=10, capture_output=True)
    except Exception:
        pass


@contextmanager
def single_instance():
    """Only one dispatcher may run at a time. launchd coalesces StartInterval
    ticks, but a manual --once/--job during a heartbeat could otherwise overlap
    and race the seen()-then-enqueue dedupe. A non-blocking exclusive lock makes
    a second concurrent invocation exit cleanly instead of double-firing."""
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    lockfile = config.STATE_DIR / "dispatcher.lock"
    fh = open(lockfile, "w")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log.info("another dispatcher holds the lock; skipping this tick")
            yield False
            return
        yield True
    finally:
        fh.close()


def _setup_logging() -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(config.LOG_DIR / "orchestrator.log"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def tick(*, dry_run: bool = False, only: str | None = None) -> list[dict]:
    now = now_local()
    # On a scheduled dark-wake, force the display off immediately and make sure we
    # have internet before any job runs. Gated to the wake windows so a normal
    # daytime tick never blanks the screen or churns Wi-Fi on you.
    if not dry_run and _in_dark_window(now):
        _display_off()
        _ensure_network()
    ledger = Ledger()
    # (Keep-awake is handled by the root wake-armer's `pmset -a disablesleep`, not
    # here — caffeinate can't beat clamshell sleep and the user agent isn't root.)
    ctx = {"now": now, "dry_run": dry_run, "ledger": ledger}
    results: list[dict] = []

    for job in JOBS:
        if only and job.name != only:
            continue
        due = (job.name == only) or job.due(now, ledger)
        if not due:
            continue
        log.info("running job=%s slot=%s dry_run=%s", job.name, job.slot(now), dry_run)
        try:
            summary = job.fn(ctx)
            results.append(summary)
            log.info("done job=%s → %s", job.name, json.dumps(summary)[:400])
            # Mark once-jobs on success — including forced runs, so a manual
            # --job before the natural slot makes the later heartbeat a no-op
            # (idempotent) rather than a second run.
            if job.is_once() and not dry_run:
                ledger.mark(job.name, job.slot(now))
        except Exception as exc:  # one job must never kill the heartbeat
            log.exception("job=%s FAILED: %s", job.name, exc)
            results.append({"job": job.name, "error": str(exc)})
    return results


def run_selftest() -> int:
    """Verify the overnight-reliability machinery WITHOUT mutating anything.
    Runnable any time: confirms the wake is armed, the scripts are valid, sleep
    isn't stuck disabled, internet is reachable, and the daemon is installed."""
    wake = config.HERE / "wake"
    arm, net = wake / "arm_wakes.sh", wake / "ensure_network.sh"
    daemon = Path("/Library/LaunchDaemons/com.example.claude-wake-armer.plist")
    checks: list[tuple[str, bool, str]] = []

    def run(cmd, t=10):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=t)
        except Exception:
            return None

    sched = (run(["/usr/bin/pmset", "-g", "sched"]) or None)
    sched_out = sched.stdout if sched else ""
    checks.append(("pmset repeat wake 00:45 armed",
                   "0:45" in sched_out or "00:45" in sched_out,
                   "fix: sudo pmset repeat wake MTWRFSU 00:45:00"))

    for label, p in (("arm_wakes.sh", arm), ("ensure_network.sh", net)):
        ok = p.exists() and os.access(p, os.X_OK) and \
            (run(["/bin/bash", "-n", str(p)]) or subprocess.CompletedProcess([], 1)).returncode == 0
        checks.append((f"{label} present + executable + valid", ok, str(p)))

    checks.append(("wake-armer LaunchDaemon installed", daemon.exists(),
                   str(daemon) + ("" if daemon.exists() else "  — sudo cp + launchctl load")))

    g = run(["/usr/bin/pmset", "-g"])
    sd = [l for l in (g.stdout.splitlines() if g else []) if "SleepDisabled" in l]
    stuck = bool(sd) and sd[0].split()[-1] == "1"
    checks.append(("sleep not stuck disabled (no crashed hold)", not stuck,
                   sd[0].strip() if sd else "SleepDisabled absent (= normal)"))

    online = (run(["/usr/bin/curl", "-fs", "-m", "6", "-o", "/dev/null",
                   "http://captive.apple.com/hotspot-detect.html"]) or
              subprocess.CompletedProcess([], 1)).returncode == 0
    checks.append(("internet reachable now", online, ""))

    checks.append(("config: Wi-Fi iface + hotspot fallbacks set",
                   bool(config.WIFI_IFACE) and bool(config.HOTSPOT_SSIDS),
                   f"{config.WIFI_IFACE} → {config.HOTSPOT_SSIDS}"))

    all_ok = True
    for name, ok, detail in checks:
        all_ok &= ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    npass = sum(1 for _, ok, _ in checks if ok)
    print(f"\n{'ALL PASS' if all_ok else 'SOME FAILED'} — {npass}/{len(checks)}")
    return 0 if all_ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Unified Claude scheduler heartbeat")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--job", help="force-run a single job by name")
    ap.add_argument("--list", action="store_true", help="print the manifest and exit")
    ap.add_argument("--selftest", action="store_true",
                    help="verify the overnight-reliability machinery and exit")
    args = ap.parse_args(argv)

    if args.selftest:
        return run_selftest()

    _setup_logging()
    if args.list:
        for j in JOBS:
            when = (f"weekday={j.weekday} at {j.at}" if j.kind == "weekly_once"
                    else f"at {j.at}" if j.kind == "daily_once"
                    else f"window {j.window}")
            print(f"  {j.name:22} [{j.kind}] {when}")
        return 0

    with single_instance() as acquired:
        if not acquired:
            return 0
        results = tick(dry_run=args.dry_run, only=args.job)
        if not results:
            log.info("tick: nothing due at %s", now_local().isoformat())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
