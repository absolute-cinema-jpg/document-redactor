"""
Desktop launcher: the Bureau in its own window. No terminal, no browser.

The same local server runs inside this process on a private, randomly chosen
port. The window is a native WebKit view in private mode, so nothing is cached
or stored on disk. Closing the window shuts everything down and burns any
document still in memory.
"""

import argparse
import base64
import json
import logging
import os
import secrets
import sys
import threading
import time

import webview
from werkzeug.serving import make_server

from . import server

TITLE = "Document Sanitization Bureau"


class Bridge:
    """Functions the page can call as window.pywebview.api.<name>().
    Names starting with an underscore are not exposed to the page."""

    def __init__(self):
        self._window = None

    def save_output(self, job_id):
        data = server.output_for(job_id)
        if data is None:
            return {"error": "There is no sanitized copy to save yet."}
        # A neutral name: the original filename can itself be confidential.
        name = "SANITIZED-%s.pdf" % secrets.token_hex(3).upper()
        result = self._window.create_file_dialog(
            webview.FileDialog.SAVE, save_filename=name, file_types=("PDF document (*.pdf)",))
        if not result:
            return {"cancelled": True}
        path = result if isinstance(result, str) else result[0]
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        with open(path, "wb") as fh:
            fh.write(data)
        return {"saved": os.path.basename(path)}


class _SelfTestBridge(Bridge):
    def ping(self):
        return "pong"


def _selftest(window, sample_path, out_path):
    """Drives the real window end to end, then closes it. Used by the build."""
    result = {}

    def js(code):
        return window.evaluate_js(code)

    def wait_for(code, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            value = js(code)
            if value:
                return value
            time.sleep(0.25)
        return None

    try:
        try:
            import fontTools.subset  # noqa: F401  needed by font re-subsetting
            result["fonttools"] = True
        except ImportError:
            result["fonttools"] = False
        result["page_title"] = wait_for("document.title")
        result["bridge_ready"] = bool(wait_for(
            "!!(window.pywebview && window.pywebview.api && window.pywebview.api.save_output)"))
        js("window.pywebview.api.ping().then(v => { window.__ping = v; })")
        result["bridge_call"] = wait_for("window.__ping || ''", 10)

        b64 = base64.b64encode(open(sample_path, "rb").read()).decode()
        js("""(async () => {
              const bytes = Uint8Array.from(atob('%s'), c => c.charCodeAt(0));
              await openFile(new File([bytes], 'sample.pdf', {type: 'application/pdf'}));
              $('#search').value = 'smithers'; runSearch(); markAllCandidates();
              S.els[S.els.length - 1].btn.click();
              await processDoc();
              window.__done = JSON.stringify({
                screen: document.body.dataset.screen,
                failed: S.report.checks.filter(c => !c.ok && !c.warn_only).map(c => c.label),
                pages: S.report.page_count, id: S.id });
            })().catch(e => { window.__done = JSON.stringify({error: String(e)}); });""" % b64)
        done = json.loads(wait_for("window.__done || ''", 60) or "{}")
        result["flow"] = done
        data = server.output_for(done.get("id", ""))
        if data:
            with open(out_path, "wb") as fh:
                fh.write(data)
            result["saved_bytes"] = len(data)
    except Exception as exc:  # report, don't hang the build
        result["error"] = repr(exc)
    finally:
        # Windowed apps on Windows have no console, so the report goes to a file.
        with open(out_path + ".json", "w") as fh:
            json.dump(result, fh)
        if sys.stdout is not None:
            print("SELFTEST " + json.dumps(result), flush=True)
        window.destroy()


def main():
    ap = argparse.ArgumentParser(description=TITLE)
    ap.add_argument("--selftest", nargs=2, metavar=("SAMPLE_PDF", "OUT_PDF"), help=argparse.SUPPRESS)
    args, _unknown = ap.parse_known_args()  # Finder may pass extra arguments

    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    httpd = make_server("127.0.0.1", 0, server.app, threaded=True)
    port = httpd.server_port
    server.allow_port(port)
    server.desktop_mode = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    bridge = _SelfTestBridge() if args.selftest else Bridge()
    window = webview.create_window(
        TITLE, "http://127.0.0.1:%d/" % port, js_api=bridge,
        width=1400, height=900, min_size=(980, 640),
        background_color="#26302a", text_select=True,
    )
    bridge._window = window
    try:
        if args.selftest:
            webview.start(_selftest, (window, *args.selftest), private_mode=True)
        else:
            webview.start(private_mode=True)
    finally:
        server.burn_all()
        httpd.shutdown()


if __name__ == "__main__":
    sys.exit(main())
