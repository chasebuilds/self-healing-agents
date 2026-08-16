"""Did the scheduled work actually happen? Artifact first, scheduler second.

The failure this exists for is six days of total silence, where nothing ran,
nothing alarmed, and the gap was only found by a human noticing the absence
of output.

**A job that stops running looks exactly like a job with nothing to
report** — and every dashboard stays green, because the dashboards are
written BY the thing that stopped.

Two independent signals, in this order:

1. **A heartbeat file the job writes itself, at the END of a run.** This is
   the one that matters. It proves the code reached the end — not that the
   scheduler believes it launched something, and not that a process exited
   0. Exit codes are especially weak evidence: plenty of CLIs print a fatal
   error to stdout and still exit 0.

2. **The scheduler's own view**, for the *why*. On Windows this reads
   through PowerShell's `Get-ScheduledTaskInfo` rather than `schtasks`,
   because `schtasks` prints localized column headings and a parser keyed
   on English silently misreads them on any non-English machine. This
   signal is diagnostic only: it explains a missing heartbeat (task
   disabled, last result nonzero, never ran), never whether the work
   happened.

A stale heartbeat next to a healthy-looking scheduled task is the
interesting case, and it is exactly what those six days looked like.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone

BEAT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "heartbeats")


def _now():
    return datetime.now(timezone.utc)


def beat(name, detail=None, beat_dir=None):
    """Record that `name` completed a run. Call this at the END of the job.

    At the end, deliberately: a heartbeat written on entry would prove the
    process started, which is the thing that was never in doubt.
    """
    d = beat_dir or BEAT_DIR
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "%s.json" % name)
    payload = {"name": name, "ts": _now().isoformat(timespec="seconds"),
               "detail": detail or {}}
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(payload, f, indent=1)
            f.write("\n")
        os.replace(tmp, path)
    except OSError:
        pass  # a heartbeat that cannot be written must not kill the run
    return payload


def last_beat(name, beat_dir=None):
    try:
        with open(os.path.join(beat_dir or BEAT_DIR, "%s.json" % name),
                  encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def hours_since_beat(name, beat_dir=None):
    got = last_beat(name, beat_dir)
    if not got:
        return None
    try:
        return (_now() - datetime.fromisoformat(got["ts"])).total_seconds() / 3600.0
    except (KeyError, ValueError):
        return None


def task_info(task_name, timeout=20):
    """Windows Task Scheduler's view, or None. Diagnostic only.

    Uses PowerShell because `Get-ScheduledTaskInfo` returns stable property
    names. Returns None on any non-Windows platform, which is correct — the
    heartbeat is the real signal and it is portable.
    """
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
           "$ErrorActionPreference='Stop';"
           "$t = Get-ScheduledTask -TaskName '%s';"
           "$i = $t | Get-ScheduledTaskInfo;"
           "[pscustomobject]@{State=[string]$t.State; LastRunTime=[string]$i.LastRunTime;"
           " LastTaskResult=$i.LastTaskResult; NextRunTime=[string]$i.NextRunTime}"
           " | ConvertTo-Json -Compress" % task_name.replace("'", "''")]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0 or not (p.stdout or "").strip():
        return None
    try:
        return json.loads(p.stdout)
    except ValueError:
        return None


def check(beat_name, task_name=None, max_age_h=25.0, probe_scheduler=True,
          beat_dir=None):
    """(ok, detail) for one scheduled job. Drops straight into a Probe.

    Set `max_age_h` to roughly 2x the job's cadence: one missed run is a
    reboot or a busy machine, two in a row is a fault.
    """
    age = hours_since_beat(beat_name, beat_dir)
    info = task_info(task_name) if (probe_scheduler and task_name) else None
    sched = ""
    if info:
        sched = " [task %s, last result %s, last run %s]" % (
            info.get("State"), info.get("LastTaskResult"),
            (info.get("LastRunTime") or "never")[:19])
        if str(info.get("State")).lower() == "disabled":
            return False, "task %s is DISABLED%s" % (task_name, sched)

    # A job running RIGHT NOW has not failed to write a heartbeat; it has
    # not got there yet. Heartbeats are written at the END of a run by
    # design, which means any long job is guaranteed to be mid-flight when
    # some probe looks at it. Without this, a job that takes 45 minutes is
    # reported as "starting and not finishing" while it is visibly working,
    # and the first thing anyone does with a watchdog that cries wolf is
    # stop reading it.
    running = info and str(info.get("State")).lower() == "running"

    if age is None:
        # No heartbeat ever. Before the first run that is expected, so it is
        # only a finding once the scheduler says it has launched something.
        # A watchdog that fails on day zero teaches everyone to ignore it.
        ran = info and (info.get("LastRunTime") or "").strip() not in (
            "", "12/30/1899 12:00:00 AM")
        if running:
            return True, "first run is in progress%s" % sched
        if ran:
            return False, ("%s has run but never wrote a heartbeat - the job is "
                           "starting and not finishing%s" % (task_name, sched))
        return True, "no heartbeat yet, and the task has not run%s" % sched

    if age > max_age_h:
        if running:
            return True, ("last completed run was %.1fh ago and a run is in "
                          "progress now%s" % (age, sched))
        return False, ("last completed run was %.1fh ago, over the %.0fh bar%s"
                       % (age, max_age_h, sched))
    return True, "last run %.1fh ago%s" % (age, sched)
