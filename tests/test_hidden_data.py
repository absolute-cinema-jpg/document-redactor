"""Hostile-PDF checks: plant data everywhere a PDF can hide it, run the real pipeline, then
search the output the way an attacker would, including every raw object and decoded stream.

    .venv/bin/python tests/test_hidden_data.py

All data is synthetic. Every new inspection check also has a positive control, proving that
it fails when the data is really there.
"""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pymupdf as fitz  # noqa: E402

from redactor import engine  # noqa: E402
from redactor.engine import (RedactionJob, _flatten_marked_content, _instructions,  # noqa: E402
                             _jpeg_without_metadata, _leftover_structures, _orphan_pages)

HIDDEN = {
    "CANARYMARKED": "word the user marked",
    "CANARYFORMV": "form field value",
    "CANARYFORMDV": "form field default value",
    "CANARYFIELDNAME": "form field name",
    "CANARYTOOLTIP": "form field tooltip",
    "CANARYSTRUCTALT": "tag alt text",
    "CANARYSTRUCTACTUAL": "tag ActualText",
    "CANARYBDCACTUAL": "ActualText on a content span",
    "CANARYWITHDRAWN": "tag text belonging to a withdrawn page",
    "CANARYWDBODY": "body of a withdrawn page",
    "CANARYOCGHIDDEN": "text on a hidden layer",
    "CANARYOCGNAME": "layer name",
    "CANARYOFFPAGE": "text below the page",
    "CANARYOFFLEFT": "text left of the page",
    "CANARYCOVERED": "text under a black box",
    "CANARYWHITE": "white on white text",
    "CANARYTINY": "0.5pt text",
    "CANARYUNDERIMG": "text under an image",
    "CANARYTRANSPARENT": "fully transparent text",
    "CANARYPIECEPAGE": "page PieceInfo",
    "CANARYPIECECAT": "catalog PieceInfo",
    "CANARYPAGEXMP": "page XMP",
    "CANARYIMGXMP": "image XMP",
    "CANARYINFO": "custom Info key",
    "CANARYLABEL": "page label",
    "CANARYDEST": "named destination",
    "CANARYCATKEY": "custom catalog key",
    "CANARYPAGEKEY": "custom page key",
    "CANARYFORMKEY": "private key on a form XObject in use",
    "CANARYIMGKEY": "private key on an image in use",
    "CANARYFORMOC": "text in a form XObject on a hidden layer",
    "CANARYNESTEDOC": "hidden-layer text inside a form XObject",
}
VISIBLE = ["KEEPORDINARY", "KEEPONLAYER", "KEEPINFORM", "KEEPWHITE", "KEEPGREY", "KEEPOVERIMG"]
failures = []


def check(ok, label, detail=""):
    print("  [%s] %s%s" % ("ok" if ok else "!!", label, " - %s" % detail if detail and not ok else ""))
    if not ok:
        failures.append(label)


def add_form(doc, page, name, body, extra=""):
    # The font dictionary is inline on purpose: PyMuPDF before 1.26.6 corrupts memory on it.
    x = doc.get_new_xref()
    doc.update_object(x, "<< /Type /XObject /Subtype /Form /BBox [0 0 612 792] /Resources "
                         "<< /Font << /helv << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >> "
                         "%s >>" % extra)
    doc.update_stream(x, body, new=True)
    kind, value = doc.xref_get_key(page.xref, "Resources")
    if kind == "xref":
        doc.xref_set_key(int(value.split()[0]), "XObject/%s" % name, "%d 0 R" % x)
    else:
        doc.xref_set_key(page.xref, "Resources/XObject/%s" % name, "%d 0 R" % x)
    return x


def append_content(doc, page, data):
    fresh = doc.get_new_xref()
    doc.update_object(fresh, "<<>>")
    doc.update_stream(fresh, page.read_contents() + b"\n" + data, new=True)
    page.set_contents(fresh)


def build():
    doc = fitz.open()
    p = doc.new_page(width=612, height=792)
    p.insert_text((72, 80), "Ordinary line KEEPORDINARY here", fontsize=12)
    p.insert_text((72, 110), "Name: CANARYMARKED end", fontsize=12)
    p.insert_text((72, 140), "Under box CANARYCOVERED", fontsize=12)
    p.draw_rect(fitz.Rect(70, 128, 300, 146), color=(0, 0, 0), fill=(0, 0, 0))
    p.insert_text((72, 170), "CANARYWHITE", fontsize=12, color=(1, 1, 1))
    p.insert_text((72, 200), "CANARYTINY", fontsize=0.5)
    p.insert_text((72, 900), "CANARYOFFPAGE", fontsize=12)
    p.insert_text((-300, 215), "CANARYOFFLEFT", fontsize=12)
    p.insert_text((400, 430), "CANARYUNDERIMG", fontsize=12)
    p.insert_text((72, 215), "CANARYTRANSPARENT", fontsize=12, fill_opacity=0.0, stroke_opacity=0.0)
    # Controls that must stay readable: white on dark, faint grey, text on top of an image.
    p.draw_rect(fitz.Rect(70, 600, 300, 620), color=(0, 0, 0), fill=(0, 0, 0))
    p.insert_text((72, 615), "KEEPWHITE", fontsize=12, color=(1, 1, 1))
    p.insert_text((72, 650), "KEEPGREY", fontsize=12, color=(0.8, 0.8, 0.8))
    pm = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 60, 60), False)
    pm.set_rect(pm.irect, (180, 200, 220))
    p.insert_image(fitz.Rect(390, 400, 560, 500), pixmap=pm)
    p.insert_text((400, 470), "KEEPOVERIMG", fontsize=12)
    img_xref = p.get_images()[0][0]
    mx = doc.get_new_xref()
    doc.update_object(mx, "<< /Type /Metadata /Subtype /XML >>")
    doc.update_stream(mx, b"<x>CANARYIMGXMP</x>", new=True)
    doc.xref_set_key(img_xref, "Metadata", "%d 0 R" % mx)
    doc.xref_set_key(img_xref, "PrivateNote", "(CANARYIMGKEY)")
    # layers
    off = doc.add_ocg("CANARYOCGNAME", on=False)
    on = doc.add_ocg("VisibleLayer", on=True)
    p.insert_text((72, 230), "CANARYOCGHIDDEN", fontsize=12, oc=off)
    p.insert_text((72, 245), "KEEPONLAYER", fontsize=12, oc=on)
    # form fields
    w = fitz.Widget()
    w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
    w.field_name = "CANARYFIELDNAME"
    w.field_value = "CANARYFORMV"
    w.field_label = "CANARYTOOLTIP"
    w.rect = fitz.Rect(72, 300, 300, 325)
    p.add_widget(w)
    doc.xref_set_key(next(p.widgets()).xref, "DV", "(CANARYFORMDV)")
    px = p.xref
    doc.xref_set_key(px, "PieceInfo", "<</App<</Private (CANARYPIECEPAGE)>>>>")
    doc.xref_set_key(px, "CustomPageKey", "(CANARYPAGEKEY)")
    pmx = doc.get_new_xref()
    doc.update_object(pmx, "<< /Type /Metadata /Subtype /XML >>")
    doc.update_stream(pmx, b"<x>CANARYPAGEXMP</x>", new=True)
    doc.xref_set_key(px, "Metadata", "%d 0 R" % pmx)

    # withdrawn page, reachable the way a Word or Acrobat export keeps it reachable
    p2 = doc.new_page(width=612, height=792)
    p2.insert_text((72, 80), "Withdrawn CANARYWDBODY", fontsize=12)
    p2_xref = p2.xref
    p = doc[0]

    # form XObjects in use: visible with a private key, on a hidden layer, and with a hidden section
    add_form(doc, p, "Fm1", b"BT /helv 12 Tf 72 400 Td <%s> Tj ET" % b"KEEPINFORM".hex().encode(),
             "/PrivateNote (CANARYFORMKEY)")
    add_form(doc, p, "Fm2", b"BT /helv 12 Tf 72 420 Td <%s> Tj ET" % b"CANARYFORMOC".hex().encode(),
             "/OC %d 0 R" % off)
    fm3 = add_form(doc, p, "Fm3", b"/OC /Lay BDC BT /helv 12 Tf 72 440 Td <%s> Tj ET EMC"
                   % b"CANARYNESTEDOC".hex().encode())
    doc.xref_set_key(fm3, "Resources/Properties/Lay", "%d 0 R" % off)
    append_content(doc, p, b"q /Fm1 Do Q q /Fm2 Do Q q /Fm3 Do Q\n"
                           b"/Span <</ActualText (CANARYBDCACTUAL)>> BDC EMC")

    # tags + catalog junk
    cat = doc.pdf_catalog()
    se2, se1, root = doc.get_new_xref(), doc.get_new_xref(), doc.get_new_xref()
    doc.update_object(se2, "<< /S /Figure /P %d 0 R /Pg %d 0 R /Alt (CANARYWITHDRAWN) >>" % (se1, p2_xref))
    doc.update_object(se1, "<< /S /Sect /P %d 0 R /Pg %d 0 R /Alt (CANARYSTRUCTALT) "
                           "/ActualText (CANARYSTRUCTACTUAL) /K [%d 0 R] >>" % (root, px, se2))
    doc.update_object(root, "<< /Type /StructTreeRoot /K [%d 0 R] >>" % se1)
    doc.xref_set_key(cat, "StructTreeRoot", "%d 0 R" % root)
    doc.xref_set_key(cat, "MarkInfo", "<</Marked true>>")
    doc.xref_set_key(cat, "PieceInfo", "<</App<</Private (CANARYPIECECAT)>>>>")
    doc.xref_set_key(cat, "PageLabels", "<</Nums [0 <</P (CANARYLABEL)>>]>>")
    dest = doc.get_new_xref()
    doc.update_object(dest, "<< /CANARYDEST [%d 0 R /Fit] >>" % px)
    doc.xref_set_key(cat, "Dests", "%d 0 R" % dest)
    doc.xref_set_key(cat, "CustomCompanyKey", "(CANARYCATKEY)")
    doc.set_metadata({"title": "normal title"})
    info = doc.xref_get_key(-1, "Info")
    if info[0] == "xref":
        doc.xref_set_key(int(info[1].split()[0]), "CustomField", "(CANARYINFO)")
    data = doc.tobytes(garbage=0)
    doc.close()
    return data


def decode_strings(text):
    """Text of every (literal) and <hex> string in `text`, so encoded canaries still match."""
    parts = []
    for m in re.finditer(r"<([0-9a-fA-F\s]+)>|\(((?:[^()\\]|\\.)*)\)", text):
        if m.group(1) is not None:
            h = re.sub(r"\s", "", m.group(1))
            parts.append(bytes.fromhex(h + "0" * (len(h) % 2)).decode("latin-1"))
        else:
            parts.append(m.group(2))
    return "\n".join(parts)


def haystack(pdf):
    """Everything an attacker with a hex editor or a PDF library gets."""
    d = fitz.open(stream=pdf, filetype="pdf")
    parts = [pdf.decode("latin-1")]
    for x in range(1, d.xref_length()):
        try:
            parts.append(d.xref_object(x, compressed=False))
            if d.xref_is_stream(x):
                parts.append(d.xref_stream(x).decode("latin-1"))
        except Exception:
            pass
    parts.append(d.xref_object(-1) if False else "")
    raw = "\n".join(parts)
    full = "\n".join([raw, decode_strings(raw)] + [p.get_text() for p in d]
                     + ["".join(chr(c[0]) for s in p.get_texttrace() for c in s["chars"]) for p in d])
    d.close()
    return full


def visible_diff(a, b):
    return sum(1 for x, y in zip(a.samples, b.samples) if abs(x - y) > 40)


def test_canaries():
    print("\n-- planted data, one page withdrawn --")
    src = build()
    planted = haystack(src)
    check(all(c in planted for c in HIDDEN) and all(k in planted for k in VISIBLE), "all data planted in the input",
          str([c for c in list(HIDDEN) + VISIBLE if c not in planted]))
    job = RedactionJob(src)
    summary = job.page_info()["summary"]
    check(summary["tagged"] and summary["hidden_layers"] == 1 and summary["concealed_text"] >= 4
          and summary["past_the_edge"] >= 1 and summary["form_fields"] == 1, "intake reports the hidden cargo", str(summary))
    idx = [i for i, w in enumerate(job.words[0]) if "CANARYMARKED" in w[4]]
    report = job.process([{"page": 0, "type": "words", "words": idx, "method": "blackout"}], remove_pages=[1])
    for c in report["checks"]:
        print("     %s %s: %s" % ("PASS" if c["ok"] else "FAIL", c["label"], c["detail"]))
    check(all(c["ok"] or c.get("warn_only") for c in report["checks"]), "every inspection check passes")
    out = haystack(job.output)
    for canary, what in HIDDEN.items():
        check(canary not in out, "gone: %s" % what)
    text = "\n".join(report["text_layer"])
    for keep in VISIBLE:
        check(keep in text, "still there: %s" % keep)
    doc = fitz.open(stream=job.output, filetype="pdf")
    check(doc.page_count == 1 and not _orphan_pages(doc), "withdrawn page object gone")
    check(not _leftover_structures(doc), "no hidden structures", str(_leftover_structures(doc)))
    doc.close()
    job.close()

    print("\n-- planted data, nothing withdrawn --")
    job = RedactionJob(src)
    report = job.process([])
    out = haystack(job.output)
    check(all(c["ok"] or c.get("warn_only") for c in report["checks"]), "every inspection check passes")
    survivors = [c for c in HIDDEN if c in out and c not in ("CANARYMARKED", "CANARYWDBODY")]
    check(not survivors, "all hidden data gone", str(survivors))
    check("CANARYWDBODY" in "\n".join(report["text_layer"]), "unmarked page stays when not withdrawn")
    job.close()


def test_withdrawn_recovery():
    print("\n-- withdrawn page in a tagged PDF: try to get it back --")
    d = fitz.open()
    d.new_page().insert_text((72, 80), "Page one is fine to share", fontsize=12)
    d.new_page().insert_text((72, 80), "CONFIDENTIALSALARY of the withdrawn page", fontsize=12)
    cat, pg = d.pdf_catalog(), d.page_xref(1)
    el, root = d.get_new_xref(), d.get_new_xref()
    d.update_object(el, "<< /S /P /P %d 0 R /Pg %d 0 R >>" % (root, pg))
    d.update_object(root, "<< /Type /StructTreeRoot /K [%d 0 R] >>" % el)
    d.xref_set_key(cat, "StructTreeRoot", "%d 0 R" % root)
    job = RedactionJob(d.tobytes())
    report = job.process([], remove_pages=[1])
    out = job.output_doc
    check(not _orphan_pages(out), "no page object to re-attach")
    check("CONFIDENTIALSALARY" not in haystack(job.output), "withdrawn text unrecoverable")
    check(next(c for c in report["checks"] if c["label"] == "Withdrawn pages destroyed")["ok"], "check agrees")
    job.close()


def test_positive_controls():
    print("\n-- the new checks fail when the data is really there --")
    # An unsanitized copy of the hostile file goes straight to the checks.
    job = RedactionJob(build())
    job.process([])
    src = job.doc
    check(_orphan_pages(src) == [], "control: live pages aren't orphans")
    src2 = fitz.open(stream=job._data, filetype="pdf")
    src2.select([0])
    orphaned = fitz.open(stream=src2.tobytes(garbage=4), filetype="pdf")
    check(bool(_orphan_pages(orphaned)), "orphan page detected (tagged PDF, page dropped, no structure strip)")
    orphaned.close()
    src2.close()
    left = _leftover_structures(src)
    check({"StructTreeRoot", "AcroForm", "OCProperties"} <= left, "hidden structures detected", str(left))
    job.output = job._data
    job.output_doc.close()
    job.output_doc = fitz.open(stream=job._data, filetype="pdf")
    job._kept = list(range(job.doc.page_count))
    verdict = job._check_hidden_data(job.output_doc, {})
    check(not verdict["ok"] and "concealed text" in verdict["detail"] and "outside the page" in verdict["detail"]
          and "marked content" in verdict["detail"], "inspection check fails on the unsanitized file", verdict["detail"])
    job.close()


def test_soft_mask():
    print("\n-- shape stored only in an image's transparency mask --")
    doc = fitz.open()
    p = doc.new_page(width=300, height=300)
    s = bytearray(100 * 100)
    for y in range(100):
        for x in range(100):
            s[y * 100 + x] = 255 if 40 <= y < 60 and 10 <= x < 90 else 128
    base = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 100, 100), False)
    base.clear_with(0)
    p.insert_image(fitz.Rect(0, 0, 300, 300), pixmap=fitz.Pixmap(base, fitz.Pixmap(fitz.csGRAY, 100, 100, bytes(s), False)))
    job = RedactionJob(doc.tobytes())
    job.process([{"page": 0, "type": "area", "rect": [0, 100, 300, 200], "method": "blackout"}])
    d = job.output_doc
    item = d[0].get_images(full=True)[0]
    mask = fitz.Pixmap(d, item[1])
    pixels = mask.samples
    secret = sum(1 for y in range(40, 60) for x in range(10, 90) if pixels[y * mask.width + x] > 128)
    outside = sum(1 for y in range(0, 25) for x in range(100) if pixels[y * mask.width + x] > 0)
    check(secret == 0, "mask pixels under the mark wiped", "%d remain" % secret)
    check(outside == 2500, "mask pixels away from the mark kept", "%d of 2500" % outside)
    job.close()


def test_rendering_kept():
    print("\n-- a normal page looks the same afterwards --")
    d = fitz.open()
    p = d.new_page(width=612, height=792)
    p.insert_text((72, 80), "A normal paragraph of ordinary text", fontsize=12)
    p.draw_rect(fitz.Rect(70, 100, 400, 200), color=(0.2, 0.4, 0.8), fill=(0.9, 0.9, 1))
    p.insert_text((80, 150), "text on a tinted box", fontsize=11, color=(0.1, 0.1, 0.1))
    on = d.add_ocg("Layer", on=True)
    p.insert_text((72, 300), "on a visible layer", fontsize=12, oc=on)
    pm = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 50, 50), False)
    pm.set_rect(pm.irect, (10, 150, 90))
    p.insert_image(fitz.Rect(72, 400, 200, 500), pixmap=pm)
    rotated = d.new_page(width=612, height=792)
    rotated.insert_text((72, 80), "On a rotated page", fontsize=12)
    rotated.set_rotation(90)
    job = RedactionJob(d.tobytes())
    report = job.process([])
    check(all(c["ok"] or c.get("warn_only") for c in report["checks"]), "inspection checks pass")
    for i in range(2):
        before = job.doc[i].get_pixmap(alpha=False, annots=False)
        after = job.output_doc[i].get_pixmap(alpha=False, annots=False)
        check((before.width, before.height) == (after.width, after.height) and visible_diff(before, after) == 0,
              "page %d renders identically" % (i + 1))
    check("On a rotated page" in report["text_layer"][1], "text on a rotated page kept")
    job.close()

    # rotated and cropped pages must not lose visible text to the off-page sweep
    d = fitz.open()
    p = d.new_page(width=612, height=792)
    p.insert_text((100, 120), "INSIDECROP", fontsize=12)
    p.insert_text((100, 700), "OUTSIDECROP", fontsize=12)
    p.set_cropbox(fitz.Rect(50, 50, 500, 400))
    r = d.new_page(width=612, height=792)
    r.insert_text((100, 120), "ROTATEDTEXT", fontsize=12)
    r.insert_text((100, 900), "ROTATEDOFF", fontsize=12)
    r.set_rotation(90)
    job = RedactionJob(d.tobytes())
    report = job.process([])
    text, out = "\n".join(report["text_layer"]), haystack(job.output)
    check("INSIDECROP" in text and "ROTATEDTEXT" in text, "visible text on cropped and rotated pages kept", text)
    check("OUTSIDECROP" not in out and "ROTATEDOFF" not in out, "text outside a crop or page removed")
    job.close()


def test_scrambled_over_cover_not_flagged():
    print("\n-- a scramble drawn over a dark box isn't reported as concealed --")
    d = fitz.open()
    p = d.new_page()
    p.insert_text((72, 80), "SCRAMBLEME and more visible words here", fontsize=12)
    job = RedactionJob(d.tobytes())
    report = job.process([{"page": 0, "type": "words", "words": [0], "method": "scramble"}])
    check(all(c["ok"] or c.get("warn_only") for c in report["checks"]), "inspection checks pass")
    job.close()


def scramble_once(word):
    """Scramble `word` through the real pipeline. Returns whether it is still in the saved
    file, read back separately from the engine's check, and that check's verdict."""
    d = fitz.open()
    d.new_page().insert_text((72, 80), "State %s and more words" % word, fontsize=11)
    job = RedactionJob(d.tobytes())
    idx = [i for i, w in enumerate(job.words[0]) if w[4] == word]
    assert len(idx) == 1, (word, idx)
    report = job.process([{"page": 0, "type": "words", "words": idx, "method": "scramble"}])
    verdict = next(c for c in report["checks"] if c["label"] == "Marked text purged from text layer")
    saved = fitz.open(stream=job.output, filetype="pdf")
    survived = word in [w[4] for w in saved[0].get_text("words")]
    saved.close()
    job.close()
    return survived, verdict["ok"]


def test_scramble_changes_the_word():
    print("\n-- a scramble never leaves the marked word in place --")
    # Before the fix these words survived often (IL 6.66%, CA 0.96% of draws), so even the
    # rarest here would be missed about 1 run in 200 million at 2000 draws.
    measure = lambda t: fitz.get_text_length(t, fontname="helv", fontsize=11)  # noqa: E731
    for word in ("IL", "Jo", "Al", "CA"):
        width = measure(word)
        kept = sum(engine._best_fit(word, None, measure, width) == word for _ in range(2000))
        check(kept == 0, "%s changes in 2000 draws" % word, "%d kept" % kept)
    # A small pool, as a subset font can have: most sets of 48 draws include the original.
    pools = {"upper": "ILMNO", "lower": "aeiou", "digits": "01234"}
    kept = sum(engine._best_fit("IL", pools, measure, measure("IL")) == "IL" for _ in range(2000))
    check(kept == 0, "IL changes in 2000 draws from a small pool", "%d kept" % kept)
    results = {engine._best_fit("I", {"upper": "IJKLM"}, measure, measure("I"), tries=1) for _ in range(500)}
    check(results == set("JKLM"), "a draw of the original is replaced even on the last try", str(results))

    # End to end: would have been missed about 1 run in 250 million at the old 6.25%.
    runs = [scramble_once("IL") for _ in range(300)]
    check(not any(s for s, _ok in runs), "IL gone from the saved file in 300 scrambles",
          "%d survived" % sum(s for s, _ok in runs))
    check(all(ok for _s, ok in runs), "the check passes on all 300")

    # The fix must not make letters depend on the original, which would leak it: "IL" has to
    # stay a possible draw, and a kept result can still share a letter with the original.
    check("IL" in {engine.scramble_text("IL") for _ in range(20000)}, "scramble_text can still draw the original")
    kept = {engine._best_fit("IL", None, measure, measure("IL")) for _ in range(2000)}
    check(len(kept) >= 10, "results still vary", str(sorted(kept)))
    check(any(k[0] == "I" or k[1] == "L" for k in kept), "a result can still share a letter with the original",
          str(sorted(kept)))

    print("\n-- words with nothing to scramble --")
    for word in ("&", "---", "_", "\u00bd"):
        survived, ok = scramble_once(word)
        check(survived, "%s is kept" % word)
        check(ok, "%s isn't reported as still readable" % word)
        check(engine._best_fit(word, None, measure, measure(word)) == word, "%s comes back unchanged" % word)

    print("\n-- control: the check fails when the word really survives --")
    real = engine._best_fit
    engine._best_fit = lambda word, *_args, **_kw: word
    try:
        # "Wi", "WiI" and "Illinois" have narrow edge letters that the check's probe trims.
        for word in ("IL", "Jo", "A", "7", "Wi", "(IL)", "Lee", "WiI", "Illinois"):
            survived, ok = scramble_once(word)
            check(survived, "control: %s really survives" % word)
            check(not ok, "check fails on a surviving %s" % word)
    finally:
        engine._best_fit = real


def test_jpeg():
    print("\n-- JPEG metadata --")
    gps = b"Exif\0\0CANARYGPSLOCATION"
    app1 = b"\xff\xe1" + (len(gps) + 2).to_bytes(2, "big") + gps
    com = b"\xff\xfe" + (len(b"CANARYCOMMENT") + 2).to_bytes(2, "big") + b"CANARYCOMMENT"
    pm = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 32, 32), False)
    pm.set_rect(pm.irect, (120, 60, 200))
    jpg = pm.tobytes("jpeg")
    dirty = jpg[:2] + app1 + com + jpg[2:]
    check(_jpeg_without_metadata(jpg) is None, "JPEG with nothing to strip left alone")
    d = fitz.open()
    d.new_page().insert_image(fitz.Rect(72, 72, 172, 172), stream=dirty)
    job = RedactionJob(d.tobytes(deflate=False))
    report = job.process([])
    check(b"CANARYGPSLOCATION" not in job.output and b"CANARYCOMMENT" not in job.output, "EXIF and comment removed")
    before, after = job.doc[0].get_pixmap(alpha=False), job.output_doc[0].get_pixmap(alpha=False)
    check(visible_diff(before, after) == 0, "image renders the same")
    check(all(c["ok"] or c.get("warn_only") for c in report["checks"]), "inspection checks pass")
    job.close()


def test_stream_parser():
    print("\n-- content stream parser --")
    tricky = (b"q (a \\) string BDC ( nested ) ) Tj <48 49> Tj % comment BDC\n"
              b"[(x) -5 <41> ] TJ << /A [ << /B (c>>) >> ] >> /N DP\n"
              b"BI /W 2 /H 2 ID \x00\xffEI\xfe EI Q")
    ops = [op for op, _o, _r in _instructions(tricky)]
    check(ops == [b"q", b"Tj", b"Tj", b"TJ", b"DP", b"BI", b"ID", b"Q"], "tokenizes tricky syntax", str(ops))
    for bad in (b"(unterminated", b"<41", b"[ (a) ", b"<< /A", b"BI ID xx", b")"):
        try:
            list(_instructions(bad))
            check(False, "rejects malformed %r" % bad)
        except ValueError:
            check(True, "rejects malformed %r" % bad)
    deep = b"[" * 200000 + b"]" * 200000 + b" Tj"
    check([op for op, _o, _r in _instructions(deep)] == [b"Tj"], "hostile nesting can't overflow")
    flat = _flatten_marked_content(b"/OC /H BDC q 1 0 0 RG 0 0 m 9 9 l S (x) Tj /I Do BI ID xx EI Q EMC (y) Tj",
                                   lambda name: name == b"/H", lambda name: False)
    check(b"(x)" not in flat and b"/I Do" not in flat and b"ID" not in flat and b"(y) Tj" in flat
          and b"BDC" not in flat and b"EMC" not in flat and b"1 0 0 RG" in flat and b"n" in flat,
          "hidden section loses content but keeps graphics state", repr(flat))


def main():
    test_canaries()
    test_withdrawn_recovery()
    test_positive_controls()
    test_soft_mask()
    test_rendering_kept()
    test_scrambled_over_cover_not_flagged()
    test_scramble_changes_the_word()
    test_jpeg()
    test_stream_parser()
    print("\n%s" % ("FAIL: %d problem(s): %s" % (len(failures), failures) if failures else "PASS"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
