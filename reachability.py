"""Can the artifact you just built actually reach a human?

This is the probe class that unit tests never catch, because the pipeline
is working correctly. The output is well-formed, the dashboard is green,
and the thing is worth zero, because the channel it travels down cannot
terminate at a person.

Two real instances, from a cold-outreach system this was extracted from:

1. The lead qualifier selected businesses **with no website**. The only
   email-derivation strategy derived `info@<domain>` **from the website**.
   Zero of 160 leads were contactable, by construction. Nothing crashed.
   It was found by hand, one purchase decision away from being found the
   expensive way.

2. 22 physical mail pieces were staged, each carrying a QR code pointing
   at a domain that had never been bought. The verifier passed all 22 for
   eight days, because it proved the QR **decoded to the expected URL** and
   never asked whether that URL resolved.

Same shape both times: **a correct artifact whose delivery channel cannot
terminate at a human.** So the check lives here, once, and gets called by
every step that spends money or postage.

Every network call is a read-only GET with a short timeout. Nothing here
sends anything.
"""

from __future__ import annotations

import socket
import urllib.error
import urllib.request
from urllib.parse import urlparse

UA = "reachability-check/1.0"

# Only these prove a page is gone. A 429 or a 403 is the host ANSWERING,
# which is evidence it is up, a probe that cries "dead" on throttling gets
# muted within a week, and then the day a link really breaks nobody looks.
DEAD_CODES = (404, 410)


def host_of(url):
    return (urlparse(url).hostname or "").strip()


def dns_ok(url, timeout=5.0):
    """Does the URL's host resolve at all?

    The cheapest possible unreachability proof: an unbought domain fails
    here in milliseconds.
    """
    host = host_of(url)
    if not host:
        return False, "no host in %r" % url
    socket.setdefaulttimeout(timeout)
    try:
        socket.getaddrinfo(host, None)
        return True, "%s resolves" % host
    except socket.gaierror as e:
        return False, "%s does not resolve (%s)" % (host, e.strerror or e)
    except OSError as e:
        return False, "%s lookup failed: %s" % (host, e)


def http_ok(url, timeout=8.0, accept=(200, 301, 302, 308)):
    """Fetch the URL and report whether it terminates at a real page.

    GET, not HEAD: static hosts answer HEAD with 405 on routes they serve
    fine, so a HEAD-based check reports the site broken once it exists.
    """
    ok, detail = dns_ok(url, timeout=timeout)
    if not ok:
        return False, detail
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            code = r.getcode()
            return code in accept, "HTTP %d" % code
    except urllib.error.HTTPError as e:
        return False, "HTTP %d" % e.code
    except (urllib.error.URLError, OSError) as e:
        return False, "unreachable: %s" % (getattr(e, "reason", None) or e)


def url_alive(url, timeout=8.0):
    """(alive, detail) where only 404/410 counts as dead.

    Use this instead of `http_ok` when you are checking a third-party page
    you do not control and cannot afford false alarms on. Anything that
    answers is alive-but-unverified; only a DNS failure or an explicit
    gone-status is a finding.
    """
    ok, detail = dns_ok(url, timeout=timeout)
    if not ok:
        return False, detail
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, "HTTP %d" % r.getcode()
    except urllib.error.HTTPError as e:
        if e.code in DEAD_CODES:
            return False, "HTTP %d, the page is gone" % e.code
        return True, "HTTP %d (host up, page unverified)" % e.code
    except (urllib.error.URLError, OSError) as e:
        return False, "unreachable: %s" % (getattr(e, "reason", None) or e)


# ------------------------------------------------------------------ channel

def _valid_email(v):
    v = (v or "").strip()
    return "@" in v and "." in v.split("@")[-1] and " " not in v


def _valid_postal(v):
    # A mailing address needs a street line with a number in it.
    v = (v or "").strip()
    return len(v) >= 8 and any(c.isdigit() for c in v)


def _valid_phone(v):
    return sum(c.isdigit() for c in (v or "")) >= 10


# field in the record -> validator. Adding a channel here is how a future
# pipeline gets the 0-of-160 guard for free.
CHANNELS = {
    "mail": ("address", _valid_postal),
    "email": ("email", _valid_email),
    "sms": ("phone", _valid_phone),
    "call": ("phone", _valid_phone),
}


def channel_coverage(records, channel):
    """What fraction of a qualified population this channel can actually
    reach. Returns {channel, field, total, reachable, pct, examples}.

    **`pct == 0` on a non-empty population is the signature.** It is not a
    bug in one record, it is a contradiction between how you qualified the
    population and how you plan to contact it. Read this number before
    buying anything that assumes the channel works.
    """
    if channel not in CHANNELS:
        raise ValueError("unknown channel %r (known: %s)"
                         % (channel, ", ".join(sorted(CHANNELS))))
    field, valid = CHANNELS[channel]
    total = reachable = 0
    examples = []
    for rec in records:
        if not isinstance(rec, dict) or not (rec.get("name") or "").strip():
            continue
        total += 1
        if valid(rec.get(field)):
            reachable += 1
        elif len(examples) < 3:
            examples.append("%s: %s=%r" % (rec.get("name"), field, rec.get(field)))
    return {
        "channel": channel,
        "field": field,
        "total": total,
        "reachable": reachable,
        "pct": (100.0 * reachable / total) if total else 0.0,
        "examples": examples,
    }
