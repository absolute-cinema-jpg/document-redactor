"""Reads the report a packaged app wrote during --selftest and decides
whether the build is fit to ship. Exit code 0 = pass.

    python packaging/check_selftest.py <out.pdf.json>
"""

import json
import sys


def main(path):
    try:
        with open(path) as fh:
            r = json.load(fh)
    except (OSError, ValueError) as exc:
        print("  no self-test report (%s): the app did not start or crashed" % exc)
        return 1
    flow = r.get("flow") or {}
    problems = []
    if r.get("error"):
        problems.append("error: " + r["error"])
    if not r.get("bridge_ready") or r.get("bridge_call") != "pong":
        problems.append("window could not talk to the app (Save would not work)")
    if not r.get("fonttools"):
        problems.append("fontTools missing from the bundle")
    if flow.get("screen") != "debrief" or flow.get("error"):
        problems.append("sanitize flow did not complete: %s" % flow)
    if flow.get("failed"):
        problems.append("inspection checks failed: %s" % flow["failed"])
    if not r.get("saved_bytes"):
        problems.append("no sanitized output produced")
    print("  report:", json.dumps(r))
    if problems:
        for p in problems:
            print("  FAIL:", p)
        return 1
    print("  self-test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
