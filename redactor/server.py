"""
Local-only web server for the redactor.

- Binds to 127.0.0.1 only; requests with any other Host header are refused
  (blocks DNS-rebinding attacks from web pages open in the same browser).
- Documents live in memory only. Uploads are read straight into RAM, never
  spooled to a temp file. Nothing is logged except method + path.
- Every response is sent with Cache-Control: no-store so the browser does not
  keep rendered pages in its disk cache.
"""

import argparse
import logging
import os
import secrets
import socket
import threading
import webbrowser

from flask import Flask, Response, abort, jsonify, request, send_from_directory

from .engine import NotAPdf, PasswordRequired, RedactionJob

STATIC = os.path.join(os.path.dirname(__file__), "static")
MAX_UPLOAD = 200 * 1024 * 1024

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD

_jobs = {}
_lock = threading.Lock()
_work = threading.Lock()  # MuPDF is not thread-safe: one document operation at a time
_allowed_hosts = set()
# The desktop window's bridge (pywebview) talks to the page through eval(), so
# the desktop app relaxes that one directive. The browser version stays strict.
desktop_mode = False


@app.before_request
def _guard():
    if request.host not in _allowed_hosts:
        abort(403)
    origin = request.headers.get("Origin")
    if request.method != "GET" and origin and origin.split("://", 1)[-1] not in _allowed_hosts:
        abort(403)


@app.after_request
def _headers(resp):
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'%s; img-src 'self' blob: data:; "
        "style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'"
        % (" 'unsafe-eval'" if desktop_mode else "")
    )
    return resp


def _job(job_id):
    with _lock:
        job = _jobs.get(job_id)
    if job is None:
        abort(404)
    return job


@app.get("/")
def index():
    return send_from_directory(STATIC, "index.html")


@app.get("/static/<path:name>")
def static_file(name):
    return send_from_directory(STATIC, name)


@app.post("/api/open")
def open_doc():
    data = request.get_data(cache=False)  # raw body, held in memory
    if not data:
        return jsonify(error="No document received."), 400
    try:
        with _work:
            job = RedactionJob(data, password=request.headers.get("X-Pdf-Password") or None)
            info = job.page_info()
    except PasswordRequired:
        return jsonify(error="password", message="This document is locked."), 401
    except NotAPdf:
        return jsonify(error="That file is not a readable PDF."), 400
    job_id = secrets.token_urlsafe(16)
    with _lock:
        # One document at a time: burn anything already open.
        with _work:
            for old in _jobs.values():
                old.close()
        _jobs.clear()
        _jobs[job_id] = job
    info["id"] = job_id
    return jsonify(info)


@app.get("/api/doc/<job_id>/page/<int:pno>.png")
def page_png(job_id, pno):
    job = _job(job_id)
    out = request.args.get("out") == "1"
    zoom = min(3.0, max(0.5, float(request.args.get("zoom", 2))))
    with _work:
        if out and job.output_doc is None:
            abort(404)
        if job.doc is None or not 0 <= pno < (job.output_doc if out else job.doc).page_count:
            abort(404)
        png = job.render(pno, zoom, output=out)
    return Response(png, mimetype="image/png")


@app.post("/api/doc/<job_id>/process")
def process(job_id):
    job = _job(job_id)
    body = request.get_json(force=True, silent=True) or {}
    marks = body.get("marks") or []
    try:
        with _work:
            report = job.process(marks, keep_hidden_text=bool(body.get("keep_hidden_text")),
                                 remove_pages=body.get("remove_pages") or [])
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(report)


@app.get("/api/doc/<job_id>/download")
def download(job_id):
    job = _job(job_id)
    if job.output is None:
        abort(404)
    # A neutral name: the original filename can itself be confidential.
    name = "SANITIZED-%s.pdf" % secrets.token_hex(3).upper()
    return Response(job.output, mimetype="application/pdf",
                    headers={"Content-Disposition": 'attachment; filename="%s"' % name})


@app.post("/api/doc/<job_id>/burn")
def burn(job_id):
    with _lock:
        job = _jobs.pop(job_id, None)
    if job is not None:
        with _work:
            job.close()
    return jsonify(ok=True)


def allow_port(port):
    _allowed_hosts.update({"127.0.0.1:%d" % port, "localhost:%d" % port})


def output_for(job_id):
    """The sanitized PDF bytes for a job, or None."""
    with _lock:
        job = _jobs.get(job_id)
    return job.output if job is not None else None


def burn_all():
    with _lock:
        jobs = list(_jobs.values())
        _jobs.clear()
    with _work:
        for job in jobs:
            job.close()


def _free_port(start, tries=20):
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return None


def main():
    ap = argparse.ArgumentParser(description="Document Redactor: remove sensitive information from PDFs")
    ap.add_argument("--port", type=int, default=1961)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    port = _free_port(args.port)
    if port is None:
        raise SystemExit("  Ports %d-%d are all in use. Try --port <number>." % (args.port, args.port + 19))
    allow_port(port)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    url = "http://127.0.0.1:%d/" % port
    print("\n  DOCUMENT REDACTOR  //  FORM RD-61")
    if port != args.port:
        print("  (port %d is busy -- another copy may already be running)" % args.port)
    print("  Operating locally at %s" % url)
    print("  Nothing leaves this machine. Ctrl+C to shut down and burn all papers.\n")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
