"""One heal run against made-up local state, so you can watch the loop work.

Everything is local: a throwaway web server on 127.0.0.1, an in-memory SQLite
table, and a state folder in a temp directory. Nothing is sent anywhere.
Usage: python demo_selfheal.py
"""

import functools
import http.server
import sqlite3
import tempfile
import threading
from pathlib import Path

import reachability
from selfheal import CRITICAL, WARN, Probe, held, print_report, run


def serve(folder):
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    handler = functools.partial(Quiet, directory=folder)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return "http://127.0.0.1:%d" % httpd.server_port


def main():
    work = tempfile.mkdtemp(prefix="selfheal-demo-")
    site = Path(work, "site")
    site.mkdir()
    (site / "offer.html").write_text("<h1>Spring offer</h1>", encoding="utf-8")
    base = serve(str(site))

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE attempts (id INTEGER, status TEXT)")
    conn.executemany("INSERT INTO attempts VALUES (?, ?)",
                     [(1, "sent"), (2, "stale"), (3, "stale"), (4, "queued")])
    leads = [  # invented contacts
        {"name": "Harbor Coffee", "email": "orders@harbor.example", "phone": ""},
        {"name": "Granite Tools", "email": "", "phone": "555 0100"},
        {"name": "Fern Hollow Farm", "email": "fern hollow farm", "phone": ""},
    ]

    def stale_rows():
        n = conn.execute("SELECT COUNT(*) FROM attempts WHERE status='stale'").fetchone()[0]
        return n == 0, "%d stale rows" % n

    def clear_stale():
        conn.execute("DELETE FROM attempts WHERE status='stale'")
        return True, "cleared stale rows"

    def coverage(channel):
        cov = reachability.channel_coverage(leads, channel)
        ok = not (cov["total"] and cov["pct"] == 0)
        return ok, "%.0f%% reachable by %s (%d/%d) %s" % (
            cov["pct"], channel, cov["reachable"], cov["total"], "; ".join(cov["examples"]))

    probes = [
        Probe("delivery.landing_page", lambda: reachability.http_ok(base + "/offer.html"),
              severity=CRITICAL, why="every piece points people at this page"),
        Probe("delivery.thank_you_page", lambda: reachability.http_ok(base + "/thanks.html"),
              severity=CRITICAL, why="the form redirects here after a signup"),
        Probe("db.stale_rows", stale_rows, remedy=clear_stale, auto=True, severity=WARN,
              why="stale rows double every count on the status page"),
        Probe("cache.fresh", lambda: (False, "cache is 3 days old"),
              remedy=lambda: (True, "rebuilt cache"), auto=True, severity=WARN,
              why="a remedy that reports success without fixing anything"),
        Probe("channel.email", lambda: coverage("email"), severity=WARN,
              why="the batch is only worth what can actually be contacted"),
        Probe("channel.sms", lambda: coverage("sms"), severity=CRITICAL,
              why="0 reachable means the whole SMS batch is worth nothing"),
    ]

    root = str(Path(work, "state"))
    report = run(probes, name="demo", root=root)
    print_report(report)
    marker = held(root)
    print("\nexit code %d; downstream gate held(): %s" % (
        report["exit"], "BLOCKED" if marker else "clear"))
    if marker:
        print(marker.strip())


if __name__ == "__main__":
    main()
