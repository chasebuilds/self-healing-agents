"""Tests for the probe engine.

Weighted toward the properties that make the engine worth having, not
toward coverage. Every case here corresponds to a way a real unattended
system failed quietly.

Run:  python test_selfheal.py
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import reachability
import selfheal
import taskwatch
from selfheal import CRITICAL, ESCALATE, HEALED, PASS, Probe


def ok(detail="fine"):
    return lambda: (True, detail)


def bad(detail="broken"):
    return lambda: (False, detail)


def status_of(report, name):
    return next(s for n, _sev, s, _d, _w in report["results"] if n == name)


class TestEngine(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def run_probes(self, probes, **kw):
        return selfheal.run(probes, name="test", root=self.root, **kw)

    def test_a_remedy_that_lies_is_an_escalate_not_a_heal(self):
        """THE point of the engine. A remedy that reports success while its
        own probe still fails must never be recorded as healed — that is how
        a 'permanent guard' ends up written down as shipped when it was
        never written at all.
        """
        report = self.run_probes([
            Probe("liar", bad(), remedy=lambda: (True, "definitely fixed"), auto=True),
        ])
        self.assertEqual(status_of(report, "liar"), ESCALATE)

    def test_a_real_repair_is_verified_by_re_running_the_probe(self):
        state = {"broken": True}
        probe = Probe("fixable",
                      lambda: (not state["broken"], "state"),
                      remedy=lambda: (state.update(broken=False), (True, "repaired"))[1],
                      auto=True)
        report = self.run_probes([probe])
        self.assertEqual(status_of(report, "fixable"), HEALED)

    def test_two_failed_remedies_stop_the_loop(self):
        """Self-healing that keeps retrying is a crash loop with better
        branding."""
        attempts = []
        probe = Probe("stubborn", bad(),
                      remedy=lambda: (attempts.append(1), (False, "no"))[1], auto=True)
        for _ in range(4):
            self.run_probes([probe])
        self.assertEqual(len(attempts), selfheal.MAX_REMEDY_FAILS)

    def test_a_probe_that_raises_is_a_finding_not_a_crash(self):
        def explode():
            raise RuntimeError("kaboom")
        report = self.run_probes([Probe("raises", explode), Probe("fine", ok())])
        self.assertEqual(status_of(report, "raises"), "FAIL")
        self.assertEqual(status_of(report, "fine"), PASS)

    def test_no_auto_means_no_repair_even_when_a_remedy_exists(self):
        """`auto` is opt-in per probe precisely so an outbound path can carry
        a remedy that a human runs, and never the engine."""
        ran = []
        report = self.run_probes([
            Probe("gate", bad(), remedy=lambda: (ran.append(1), (True, "sent"))[1]),
        ])
        self.assertEqual(ran, [])
        self.assertEqual(status_of(report, "gate"), "FAIL")

    def test_critical_failure_writes_a_hold_that_downstream_can_read(self):
        self.run_probes([Probe("money", bad(), severity=CRITICAL,
                               why="would spend postage on a dead link")])
        held = selfheal.held(self.root)
        self.assertIsNotNone(held)
        self.assertIn("would spend postage", held)

    def test_the_hold_clears_when_the_critical_probe_passes(self):
        self.run_probes([Probe("money", bad(), severity=CRITICAL)])
        self.assertIsNotNone(selfheal.held(self.root))
        self.run_probes([Probe("money", ok(), severity=CRITICAL)])
        self.assertIsNone(selfheal.held(self.root))

    def test_a_warn_failure_does_not_hold(self):
        self.run_probes([Probe("cosmetic", bad())])
        self.assertIsNone(selfheal.held(self.root))

    def test_history_is_append_only(self):
        self.run_probes([Probe("a", ok())])
        self.run_probes([Probe("a", ok())])
        with open(os.path.join(self.root, "heal_history.jsonl"), encoding="utf-8") as f:
            rows = [json.loads(x) for x in f if x.strip()]
        self.assertEqual(len(rows), 2)

    def test_a_failing_notifier_does_not_fail_the_run(self):
        def boom(_report):
            raise RuntimeError("discord is down")
        report = self.run_probes([Probe("a", ok())], on_report=boom)
        self.assertEqual(report["exit"], 0)


class TestReachability(unittest.TestCase):
    def test_zero_percent_coverage_is_the_signature(self):
        """Not a bug in one record — a contradiction between how the
        population was qualified and how it will be contacted. This is the
        0-of-160 case."""
        leads = [{"name": "A", "email": None}, {"name": "B", "email": ""}]
        got = reachability.channel_coverage(leads, "email")
        self.assertEqual(got["total"], 2)
        self.assertEqual(got["pct"], 0.0)

    def test_unnamed_records_are_not_counted(self):
        got = reachability.channel_coverage([{"name": "", "email": "a@b.co"}], "email")
        self.assertEqual(got["total"], 0)

    def test_a_postal_address_needs_a_number(self):
        self.assertTrue(reachability._valid_postal("221B Baker Street"))
        self.assertFalse(reachability._valid_postal("Baker Street"))

    def test_unknown_channel_raises_rather_than_silently_passing(self):
        with self.assertRaises(ValueError):
            reachability.channel_coverage([], "carrier-pigeon")


class TestTaskwatch(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_a_beat_round_trips(self):
        taskwatch.beat("job", {"rows": 3}, beat_dir=self.dir)
        self.assertEqual(taskwatch.last_beat("job", self.dir)["detail"]["rows"], 3)

    def test_stale_beat_fails(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat(timespec="seconds")
        with open(os.path.join(self.dir, "job.json"), "w", encoding="utf-8") as f:
            f.write('{"name": "job", "ts": "%s", "detail": {}}' % old)
        good, detail = taskwatch.check("job", max_age_h=9, probe_scheduler=False,
                                       beat_dir=self.dir)
        self.assertFalse(good)
        self.assertIn("over the 9h bar", detail)

    def test_no_beat_and_no_scheduler_is_not_an_alarm(self):
        """A watchdog that fails on day zero teaches everyone to ignore it."""
        good, _ = taskwatch.check("never", max_age_h=9, probe_scheduler=False,
                                  beat_dir=self.dir)
        self.assertTrue(good)

    def test_missing_beat_is_none_not_zero(self):
        self.assertIsNone(taskwatch.hours_since_beat("never", self.dir))

    def _running(self, last_run="01/01/2026 03:00:00"):
        """Stand in for the scheduler reporting a job as currently running."""
        real = taskwatch.task_info
        taskwatch.task_info = lambda name, **kw: {
            "State": "Running", "LastTaskResult": 267009, "LastRunTime": last_run}
        return real

    def test_a_job_running_right_now_is_not_a_stale_beat(self):
        """Heartbeats are written at the END of a run, so a long job is
        always mid-flight when a probe looks at it. Reporting that as a
        fault is how a watchdog trains people to ignore it."""
        old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat(timespec="seconds")
        with open(os.path.join(self.dir, "job.json"), "w", encoding="utf-8") as f:
            f.write('{"name": "job", "ts": "%s", "detail": {}}' % old)
        real = self._running()
        try:
            good, detail = taskwatch.check("job", "SomeTask", max_age_h=9,
                                           beat_dir=self.dir)
        finally:
            taskwatch.task_info = real
        self.assertTrue(good, detail)
        self.assertIn("in progress", detail)

    def test_a_first_run_in_progress_is_not_a_job_that_never_finishes(self):
        real = self._running()
        try:
            good, detail = taskwatch.check("never", "SomeTask", max_age_h=9,
                                           beat_dir=self.dir)
        finally:
            taskwatch.task_info = real
        self.assertTrue(good, detail)
        self.assertIn("first run", detail)

    def test_a_stale_beat_with_no_run_in_progress_still_fails(self):
        """The fix must not swallow the finding it exists to report."""
        old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat(timespec="seconds")
        with open(os.path.join(self.dir, "job.json"), "w", encoding="utf-8") as f:
            f.write('{"name": "job", "ts": "%s", "detail": {}}' % old)
        real = taskwatch.task_info
        taskwatch.task_info = lambda name, **kw: {
            "State": "Ready", "LastTaskResult": 0, "LastRunTime": "01/01/2026 03:00:00"}
        try:
            good, detail = taskwatch.check("job", "SomeTask", max_age_h=9,
                                           beat_dir=self.dir)
        finally:
            taskwatch.task_info = real
        self.assertFalse(good)
        self.assertIn("over the 9h bar", detail)


if __name__ == "__main__":
    unittest.main(verbosity=2)
