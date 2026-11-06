"""End-to-end checks for the redaction engine.

    .venv/bin/python tests/test_engine.py [output_dir]
"""

import os
import random
import re
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pymupdf as fitz  # noqa: E402

from make_sample import build  # noqa: E402
from redactor import engine  # noqa: E402
from redactor.engine import RedactionJob  # noqa: E402

# Scrambles normally draw from OS entropy, and about 1 run in 100 one lands on a value in
# SECRETS by chance. A fixed seed gives the same draws every run, so a failure is a real one.
engine._rng = random.Random(1961)

SECRETS = ["Jonathan", "Smithers", "1428", "Springfield", "62704", "555-0143",
           "j.smithers@example.com", "2291", "4471", "0093", "03/14/2025",
           "Jane Adjuster", "fraud", "123-45-6789", "crm.internal", "notes.txt"]


def stream_strings(doc):
    for xref in range(1, doc.xref_length()):
        if not doc.xref_is_stream(xref):
            continue
        try:
            data = doc.xref_stream(xref) or b""
        except Exception:
            continue
        for m in re.finditer(rb"<([0-9a-fA-F\s]+)>|\(((?:[^()\\]|\\.)*)\)", data):
            if m.group(1) is not None:
                h = re.sub(rb"\s", b"", m.group(1))
                yield xref, bytes.fromhex((h + b"0" * (len(h) % 2)).decode())
            else:
                yield xref, m.group(2)


def stream_secrets(doc):
    found = set()
    for xref, raw in stream_strings(doc):
        text = raw.decode("latin-1")
        for sec in SECRETS:
            if sec in text:
                found.add((sec, xref))
    return found


def find_word(words, text):
    return next(i for i, w in enumerate(words) if w["t"].replace("­", "-") == text)


def main(out_dir):
    src = os.path.join(out_dir, "sample.pdf")
    build(src)
    job = RedactionJob(open(src, "rb").read())
    info = job.page_info()
    p0, p1 = info["pages"][0]["words"], info["pages"][1]["words"]

    every_smithers = [i for i, w in enumerate(p0) if "Smithers" in w["t"] or "smithers" in w["t"]]
    marks = [
        {"page": 0, "type": "words", "method": "blackout",
         "words": [find_word(p0, "Jonathan"), find_word(p0, "Q.")] + every_smithers},
        {"page": 0, "type": "words", "method": "scramble",
         "words": [find_word(p0, t) for t in ("1428", "Elm", "Street,", "Springfield,", "IL",
                                              "62704", "(217)", "555-0143",
                                              "PX-2291-4471-0093", "03/14/2025")]},
        {"page": 0, "type": "area", "method": "blackout", "rect": info["pages"][0]["images"][0]},
        {"page": 1, "type": "words", "method": "blackout",
         "words": [find_word(p1, "Smithers"), find_word(p1, "PX-2291-4471-0093")]},
    ]
    report = job.process(marks)

    for c in report["checks"]:
        print("  [%s] %-40s %s" % ("ok" if c["ok"] else ("!!" if not c.get("warn_only") else "~~"),
                                   c["label"], c["detail"]))
    print("  leaks:", [l["term"] for l in report["leaks"]])

    out_path = os.path.join(out_dir, "sample-REDACTED.pdf")
    open(out_path, "wb").write(job.output)
    for i in range(report["page_count"]):
        open(os.path.join(out_dir, "out%d.png" % i), "wb").write(job.render(i, 1.5, output=True))

    # Independent check: grep the raw bytes + all extracted text for secrets.
    raw = job.output
    out = fitz.open(stream=raw, filetype="pdf")
    text = "\n".join(p.get_text() for p in out)
    text += "\n".join(str(v) for v in out.metadata.values())
    expected_left = {"Smithers"}  # "Mr. Smithers" / "J. Q. Smithers" not all selected
    failures = []
    for s in SECRETS:
        in_text = s in text
        # Short strings turn up by chance in compressed bytes; streams are searched below.
        in_raw = len(s) >= 8 and s.encode() in raw
        if (in_text or in_raw) and s not in expected_left:
            failures.append((s, in_text, in_raw))
    # Decompressed streams: decode every (literal) and <hex> string and look for
    # secrets. Covers single-byte fonts; glyph-ID fonts are covered by the text
    # extraction above, since MuPDF decodes those through the font's CMap.
    failures += [(sec, "stream", x) for sec, x in stream_secrets(out) if sec not in expected_left]
    hard_fail = [c for c in report["checks"] if not c["ok"] and not c.get("warn_only")]
    print("  scrambled text sample:", [l for l in out[0].get_text().splitlines() if l.strip()][-6:])
    if failures or hard_fail:
        print("FAIL", failures, [c["label"] for c in hard_fail])
        return 1
    print("PASS ->", out_path)
    return test_scanned(out_dir, src)


def test_scanned(out_dir, src_path):
    """An image-only page with an invisible OCR text layer on top."""
    print("\n  -- scanned page with OCR layer --")
    src = fitz.open(src_path)
    scan = fitz.open()
    page = scan.new_page(width=src[0].rect.width, height=src[0].rect.height)
    page.insert_image(page.rect, pixmap=src[0].get_pixmap(dpi=150, annots=False))
    for w in src[0].get_text("words"):
        page.insert_text((w[0], w[3] - 3), w[4].replace("\u00ad", "-"), fontsize=10, render_mode=3)
    job = RedactionJob(scan.tobytes())
    info = job.page_info()
    summary = info["summary"]
    print("  intake: scanned_pages=%s invisible_spans=%d" % (summary["scanned_pages"], summary["invisible_spans"]))
    words = info["pages"][0]["words"]
    idx = [i for i, w in enumerate(words) if w["t"] in ("Jonathan", "Smithers")]
    scr = [i for i, w in enumerate(words) if w["t"] == "03/14/2025"]
    marks = [{"page": 0, "type": "words", "words": idx, "method": "blackout"},
             {"page": 0, "type": "words", "words": scr, "method": "scramble"}]

    report = job.process(marks)
    text = report["text_layer"][0]
    leak_terms = [l["term"] for l in report["leaks"]]
    print("  leaks still visible in the scan image:", leak_terms)
    open(os.path.join(out_dir, "scan_out.png"), "wb").write(job.render(0, 1.2, output=True))
    hidden_check = next(c for c in report["checks"] if "Invisible" in c["label"])
    print("  [%s] %s: %s" % ("ok" if hidden_check["ok"] else "!!", hidden_check["label"], hidden_check["detail"]))
    print("  text layer after default sanitize:", repr(text.strip()[:80]))
    ok = summary["scanned_pages"] == [1] and hidden_check["ok"] and "Claimant" not in text
    ok = ok and "Smithers" in leak_terms  # "Mr. Smithers" is still printed in the image
    # the scrambled replacement is visible text and must survive the OCR strip
    ok = ok and any(ch.isdigit() for ch in text)

    report = job.process(marks, keep_hidden_text=True)
    text = report["text_layer"][0]
    print("  keep_hidden_text: Claimant kept=%s, Jonathan gone=%s" % ("Claimant" in text, "Jonathan" not in text))
    ok = ok and "Claimant" in text and "Jonathan" not in text
    print("PASS" if ok else "FAIL")
    if not ok:
        return 1
    return test_withdraw(src_path)


def test_withdraw(src_path):
    """Withdrawn pages must vanish completely: text, images, page count."""
    print("\n  -- withdrawing a page --")
    doc = fitz.open(src_path)
    page = doc.new_page()
    page.insert_text((72, 100), "Appendix: Zeppelin-9981 medical history of claimant", fontsize=11)
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 40, 40), False)
    pix.set_rect(pix.irect, (10, 200, 30))  # an image used only on this page
    page.insert_image(fitz.Rect(72, 120, 152, 200), pixmap=pix)
    job = RedactionJob(doc.tobytes())
    smithers = [i for i, w in enumerate(job.words[0]) if w[4] == "Smithers"]

    report = job.process([{"page": 0, "type": "words", "words": smithers, "method": "blackout"}],
                         remove_pages=[2])
    out = job.output_doc
    check = next(c for c in report["checks"] if c["label"] == "Withdrawn pages destroyed")
    print("  [%s] %s: %s" % ("ok" if check["ok"] else "!!", check["label"], check["detail"]))
    text = "".join(p.get_text() for p in out)
    ok = (check["ok"] and out.page_count == 2 and report["kept_pages"] == [0, 1]
          and "Zeppelin" not in text and b"Zeppelin" not in job.output)
    # leak occurrences must never point at a withdrawn page
    ok = ok and all(o["page"] != 2 for l in report["leaks"] for o in l["occurrences"])

    # positive control: the same check must fail when the page was NOT removed
    control = job._check_withdrawn(job.doc, [0, 1], [2])
    print("  control (page left in place) flagged:", control["detail"])
    ok = ok and not control["ok"] and "image" in control["detail"]

    try:
        job.process([], remove_pages=[0, 1, 2])
        ok = False
    except ValueError:
        print("  refusing to withdraw every page: ok")
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    d = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp()
    sys.exit(main(d))
