"""Detect -> remedy -> RE-VERIFY -> learn. A probe engine for unattended jobs.

`check.py`-style health scripts answer "did anything crash?". This answers
the question that actually costs money: **is what we built still able to
reach a human, and is what we are about to spend still worth spending?**

Every expensive failure this was written for answered *yes* to "nothing
crashed" and *no* to "it was worth anything". See the README for the
specific ones.

Usage:

    from selfheal import Probe, run, print_report

    probes = [
        Probe("db.reachable", check_db, severity=CRITICAL,
              why="every downstream step reads from here"),
        Probe("cache.stale", check_cache, remedy=rebuild_cache, auto=True,
              why="a stale cache serves yesterday's prices as today's"),
    ]
    report = run(probes, name="pricing-job", root="./state")
    print_report(report)
    raise SystemExit(report["exit"])

No third-party dependencies. Nothing here talks to a network by itself;
notification is a callback you supply.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import traceback
from datetime import datetime, timezone

PASS, FAIL, HEALED, ESCALATE, SKIP = "PASS", "FAIL", "HEALED", "ESCALATE", "SKIP"
CRITICAL, WARN = "critical", "warn"

# Two strikes. A third attempt is the definition of a crash loop.
MAX_REMEDY_FAILS = 2

HOLD_NAME = "DO_NOT_PROCEED.txt"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_cmd(args, cwd=None, timeout=600):
    """Convenience for remedies that shell out. Returns (returncode, output)."""
    try:
        p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        return 124, "timed out after %ds" % timeout
    except OSError as e:
        return 127, str(e)


class Probe:
    """One invariant, plus optionally how to repair it.

    check()  -> (ok: bool, detail: str)
    remedy() -> (ok: bool, detail: str)

    `why` is printed on failure, and it is not decoration: a probe whose
    message does not say what breaks in the real world is a probe the next
    person on call will mute.

    `auto=True` is opt-in per probe and should never be set on anything
    that arms an outbound path (sending, publishing, dispatching, paying).
    Keep that as a probe that only ever alarms.
    """

    def __init__(self, name, check, severity=WARN, remedy=None, auto=False,
                 why="", lesson=None):
        self.name = name
        self.check = check
        self.severity = severity
        self.remedy = remedy
        self.auto = auto
        self.why = why
        self.lesson = lesson


# ------------------------------------------------------------------ state

def _state_path(root):
    return os.path.join(root, "heal_state.json")


def _history_path(root):
    return os.path.join(root, "heal_history.jsonl")


def load_state(root):
    try:
        with open(_state_path(root), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(root, state):
    tmp = _state_path(root) + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, _state_path(root))


def _call(fn):
    """A probe that raises is itself a finding, not a reason to abort the run."""
    try:
        out = fn()
    except Exception:  # noqa: BLE001
        return False, "probe raised: %s" % traceback.format_exc(limit=2).strip().splitlines()[-1]
    if isinstance(out, tuple) and len(out) >= 2:
        return bool(out[0]), str(out[1])
    return bool(out), ""


# ------------------------------------------------------------------ engine

def run(probes, name, root, repair=True, hold=True, on_report=None, on_lesson=None):
    """Run every probe, heal what is safely healable, report the rest.

    `on_report(report)` and `on_lesson(probe, detail)` are optional
    callbacks — wire them to your own notifier and notes store. The engine
    itself performs no I/O beyond the state and history files under `root`.
    """
    os.makedirs(root, exist_ok=True)
    state = load_state(root)
    results = []

    for p in probes:
        ok, detail = _call(p.check)
        if ok:
            if state.get(p.name, {}).get("fails"):
                state[p.name] = {"fails": 0, "last_pass": _now()}
            results.append((p, PASS, detail))
            continue

        fails = int(state.get(p.name, {}).get("fails", 0))
        can_try = p.remedy and p.auto and repair and fails < MAX_REMEDY_FAILS

        if not can_try:
            reason = detail
            if p.remedy and p.auto and fails >= MAX_REMEDY_FAILS:
                reason = "%s [remedy failed %dx, not retrying]" % (detail, fails)
            results.append((p, ESCALATE if fails >= MAX_REMEDY_FAILS else FAIL, reason))
            _learn(on_lesson, p, reason)
            continue

        r_ok, r_detail = _call(p.remedy)

        # The whole point of the engine. Prove the repair worked by running
        # the probe again; do not take the remedy's word for it. A fix
        # nobody re-checked is a hope.
        v_ok, v_detail = _call(p.check)

        if r_ok and v_ok:
            state[p.name] = {"fails": 0, "last_heal": _now(), "last_remedy": r_detail[:200]}
            results.append((p, HEALED, "%s -> %s" % (detail, r_detail or "repaired")))
        else:
            state[p.name] = {"fails": fails + 1, "last_fail": _now(),
                             "last_remedy": (r_detail or v_detail)[:200]}
            why = r_detail if not r_ok else "remedy ran but probe still fails: %s" % v_detail
            results.append((p, ESCALATE, "%s | %s" % (detail, why)))
            _learn(on_lesson, p, why)

    save_state(root, state)
    _write_history(root, name, results)
    if hold:
        _hold(root, results)
    report = _summarize(name, results)
    if on_report:
        try:
            on_report(report)
        except Exception as e:  # noqa: BLE001 - a failed notification must not fail the run
            print("[selfheal] on_report failed: %s" % e, file=sys.stderr)
    return report


def _learn(on_lesson, probe, detail):
    if on_lesson and probe.lesson:
        try:
            on_lesson(probe, detail)
        except Exception as e:  # noqa: BLE001
            print("[selfheal] on_lesson failed: %s" % e, file=sys.stderr)


def _write_history(root, name, results):
    """Append-only run log. Read it to answer which invariants keep breaking
    — a probe that fails repeatedly is a design defect asking to be promoted
    into a real code-level guard."""
    with open(_history_path(root), "a", encoding="utf-8", newline="\n") as f:
        for p, status, detail in results:
            f.write(json.dumps({"ts": _now(), "job": name, "probe": p.name,
                                "severity": p.severity, "status": status,
                                "detail": detail[:400]}, ensure_ascii=False) + "\n")


def _hold(root, results):
    """An unresolved CRITICAL writes a marker downstream steps refuse to
    cross, so "broken" costs nothing instead of costing postage."""
    path = os.path.join(root, HOLD_NAME)
    blocking = [(p, s, d) for p, s, d in results
                if p.severity == CRITICAL and s in (FAIL, ESCALATE)]
    if blocking:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write("HOLD written %s by selfheal. Do not proceed.\n\n" % _now())
            for p, s, d in blocking:
                f.write("- [%s] %s\n    %s\n    why it matters: %s\n" % (s, p.name, d, p.why))
            f.write("\nClear it by fixing the above and re-running; this file is "
                    "rewritten every run.\n")
    elif os.path.exists(path):
        os.remove(path)


def held(root):
    """Downstream gate: `if selfheal.held(STATE_DIR): refuse()`."""
    path = os.path.join(root, HOLD_NAME)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return f.read()


def _summarize(name, results):
    counts = {}
    for _, status, _ in results:
        counts[status] = counts.get(status, 0) + 1
    unresolved = [(p, s, d) for p, s, d in results if s in (FAIL, ESCALATE)]
    return {
        "job": name,
        "ts": _now(),
        "counts": counts,
        "results": [(p.name, p.severity, s, d, p.why) for p, s, d in results],
        "healed": [p.name for p, s, _ in results if s == HEALED],
        "unresolved": [(p.name, p.severity, s, d) for p, s, d in unresolved],
        "blocking": [p.name for p, s, _ in results
                     if p.severity == CRITICAL and s in (FAIL, ESCALATE)],
        "exit": 1 if unresolved else 0,
    }


def print_report(report):
    for name, _sev, status, detail, why in report["results"]:
        mark = {PASS: "ok  ", HEALED: "HEAL", FAIL: "FAIL",
                ESCALATE: "ESC ", SKIP: "skip"}[status]
        print("  %s %-30s %s" % (mark, name, detail))
        if status in (FAIL, ESCALATE) and why:
            print("       why: %s" % why)
    c = report["counts"]
    print("\n%d probes: %d pass, %d healed, %d failed, %d escalated"
          % (sum(c.values()), c.get(PASS, 0), c.get(HEALED, 0),
             c.get(FAIL, 0), c.get(ESCALATE, 0)))
    if report["blocking"]:
        print("HOLD: %s (%s written, downstream blocked)"
              % (", ".join(report["blocking"]), HOLD_NAME))
