#!/usr/bin/env python3
"""
Payroll vendor outage monitor, one-shot version for GitHub Actions.
Each run checks every service once, compares with state.json, pushes alerts
through ntfy, and exits. The workflow commits state.json when it changes.
Run with --test to check everything once and send a test push (no state saved).
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
FAILS_BEFORE_ALERT = 2   # consecutive failed reachability checks (about 10 min)
HEARTBEAT_DAYS = 7       # weekly check-in push; also keeps the schedule alive
STATE_FILE = Path(__file__).parent / "state.json"
PUSH_FAILED = False

# type "statuspage": Atlassian Statuspage site (reads <url>/api/v2/summary.json)
# type "feed":       any RSS/Atom status feed (alerts on every new post)
# type "http":       reachability only (alerts on timeouts / 5xx errors)
#
# The "http" entries are placeholders: a homepage being up does NOT prove the
# payroll app is up. Swap each for the vendor's real status page/feed, or for
# the login page you actually use.
SERVICES = {
    "Rippling": {"type": "statuspage", "url": "https://status.rippling.com"},
    "Paycor":   {"type": "http", "url": "https://www.paycor.com"},
    "ADP":      {"type": "http", "url": "https://www.adp.com"},
    "Paychex":  {"type": "http", "url": "https://www.paychex.com"},
    "Paycom":   {"type": "http", "url": "https://www.paycom.com"},
    "UKG":      {"type": "http", "url": "https://www.ukg.com"},
    "Workday":  {"type": "http", "url": "https://www.workday.com"},
}


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "payroll-outage-monitor/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read()


def notify(title, msg, urgent=True):
    global PUSH_FAILED
    print(f"{title}: {msg}", flush=True)
    if not NTFY_TOPIC:
        return
    err = None
    for _ in range(3):
        try:
            req = urllib.request.Request(
                f"https://ntfy.sh/{NTFY_TOPIC}",
                data=msg.encode("utf-8"),
                headers={
                    "Title": title,  # headers must be ASCII
                    "Priority": "high" if urgent else "default",
                    "Tags": "rotating_light" if urgent else "white_check_mark",
                },
            )
            urllib.request.urlopen(req, timeout=10).close()
            return
        except Exception as e:
            err = e
            time.sleep(2)
    PUSH_FAILED = True  # makes the run fail so GitHub emails you
    print(f"  (could not send push: {err})")


def check_statuspage(url):
    d = json.loads(fetch(url.rstrip("/") + "/api/v2/summary.json"))
    desc = d["status"]["description"]
    if d["status"]["indicator"] == "none":
        return "ok", desc
    names = "; ".join(i["name"] for i in d.get("incidents", []))
    return "issue", f"{desc}. {names}".strip()


def check_http(url):
    try:
        fetch(url)
        return "ok", "reachable"
    except urllib.error.HTTPError as e:
        # 4xx is often bot-blocking, so only 5xx counts as a problem
        return ("issue" if e.code >= 500 else "ok"), f"HTTP {e.code}"
    except Exception as e:
        return "issue", f"unreachable ({e})"


def check_feed(name, url, st):
    root = ET.fromstring(fetch(url))
    items = []
    for el in root.iter():
        if el.tag.split("}")[-1] in ("item", "entry"):
            f = {c.tag.split("}")[-1]: (c.text or "").strip() for c in el}
            items.append((f.get("guid") or f.get("id") or f.get("title", ""), f.get("title", "")))
    seen = st.get("seen")  # None on first run: seed silently, don't alert
    if seen is not None:
        for item_id, title in items[:5]:
            if item_id not in seen:
                notify(f"{name}: new status post", title)
    st["seen"] = ([i for i, _ in items] + (seen or []))[:200]
    return f"{len(items)} posts in feed"


def step(name, cfg, st):
    kind = cfg["type"]
    try:
        if kind == "feed":
            now, detail = "ok", check_feed(name, cfg["url"], st)
        elif kind == "statuspage":
            now, detail = check_statuspage(cfg["url"])
        else:
            now, detail = check_http(cfg["url"])
        st["errs"] = 0
    except Exception as e:
        st["errs"] = st.get("errs", 0) + 1
        if st["errs"] == 3:
            notify(f"{name}: monitor problem",
                   f"Can't read {cfg['url']} ({e}). Check this entry in SERVICES.", urgent=False)
        return "error", str(e)

    if kind == "http":
        st["fails"] = st.get("fails", 0) + 1 if now == "issue" else 0
        if now == "issue" and st["fails"] < FAILS_BEFORE_ALERT:
            return "ok", f"{detail} (retrying)"

    prev = st.get("state", "ok")
    changed = kind == "statuspage" and detail != st.get("detail")
    if now == "issue" and (prev != "issue" or changed):
        notify(f"{name}: possible outage", detail)
    elif now == "ok" and prev == "issue":
        notify(f"{name}: recovered", detail, urgent=False)
    st["state"], st["detail"] = now, detail
    return now, detail


def main():
    global FAILS_BEFORE_ALERT
    test = "--test" in sys.argv
    state = {}
    if test:
        FAILS_BEFORE_ALERT = 1
        notify("Payroll monitor test", "If you can read this, push alerts work.", urgent=False)
    else:
        try:
            state = json.loads(STATE_FILE.read_text())
        except Exception:
            state = {}
    before = json.dumps(state, sort_keys=True)

    results = {}
    for name, cfg in SERVICES.items():
        results[name] = step(name, cfg, state.setdefault(name, {}))
        if test:
            print(f"{name:10} {results[name][0].upper():6} {results[name][1]}")

    if not test:
        meta = state.setdefault("_meta", {})
        if time.time() - meta.get("heartbeat", 0) > HEARTBEAT_DAYS * 86400:
            bad = [f"{n} ({r[0]})" for n, r in results.items() if r[0] != "ok"]
            notify("Payroll monitor: weekly check-in",
                   "Running. " + ("Needs attention: " + ", ".join(bad) if bad
                                  else "All services look normal."), urgent=False)
            meta["heartbeat"] = int(time.time())
        if json.dumps(state, sort_keys=True) != before:
            STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True))
    sys.exit(1 if PUSH_FAILED else 0)


if __name__ == "__main__":
    main()
