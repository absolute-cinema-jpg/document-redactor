"""
Redaction engine.

Everything here works on bytes held in memory. Nothing is written to disk.

Redactions are *real*: the text under a mark is deleted from the page's
content stream (via MuPDF's redaction support), image pixels under a mark are
destroyed, and the file is rebuilt from scratch on save so no old objects
survive. Only after that is anything drawn back on top -- an ink bar for a
blackout, or freshly generated random characters for a scramble.
"""

import math
import re
import secrets

import fitz  # PyMuPDF

BLACKOUT = "blackout"
SCRAMBLE = "scramble"

_rng = secrets.SystemRandom()  # OS entropy: scrambled output can't be reversed
_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_LOWER = _UPPER.lower()
_DIGITS = "0123456789"
_INK = (0.04, 0.04, 0.05)

# Words too common to be worth flagging as a possible leak.
_STOPWORDS = set(
    "the and for with from that this was are were has have had not but you your "
    "our their his her its who what when where which will would shall may can "
    "per via mr mrs ms dr inc ltd llc".split()
)

_INFO_KEYS = ("title", "author", "subject", "keywords", "creator", "producer",
              "creationDate", "modDate", "trapped")


class PasswordRequired(Exception):
    pass


class NotAPdf(Exception):
    pass


# --------------------------------------------------------------------------
# Scrambling
# --------------------------------------------------------------------------

def scramble_text(text, pools=None):
    """Replace every letter/digit with an independent random one of the same
    class. Punctuation and spacing are kept so the *shape* of the data
    (dates, emails, account numbers) survives but the content does not."""
    pools = pools or {}
    upper = pools.get("upper", _UPPER)
    lower = pools.get("lower", _LOWER)
    digits = pools.get("digits", _DIGITS)
    out = []
    for ch in text:
        if ch.isdigit():
            out.append(_rng.choice(digits))
        elif ch.isalpha():
            if ch.isupper():
                out.append(_rng.choice(upper))
            else:
                out.append(_rng.choice(lower))
        else:
            out.append(ch)
    return "".join(out)


def _base14(fontname, flags):
    n = (fontname or "").lower()
    bold = bool(flags & 16) or any(k in n for k in ("bold", "black", "heavy", "semibold"))
    italic = bool(flags & 2) or "italic" in n or "oblique" in n
    mono = bool(flags & 8) or "courier" in n or "mono" in n or "consol" in n
    serif = bool(flags & 4) or any(k in n for k in ("times", "roman", "garamond", "georgia",
                                                     "cambria", "minion", "serif"))
    if "sans" in n:
        serif = False
    family = "co" if mono else ("ti" if serif else "he")
    if family == "he":
        return {(0, 0): "helv", (1, 0): "hebo", (0, 1): "heit", (1, 1): "hebi"}[(bold, italic)]
    if family == "ti":
        return {(0, 0): "tiro", (1, 0): "tibo", (0, 1): "tiit", (1, 1): "tibi"}[(bold, italic)]
    return {(0, 0): "cour", (1, 0): "cobo", (0, 1): "coit", (1, 1): "cobi"}[(bold, italic)]


class _FontCache:
    """Tries to reuse the document's own embedded font for scrambled text, so
    the replacement looks like the original. Random characters are only drawn
    from glyphs that font actually contains. Falls back to a standard font."""

    def __init__(self, doc):
        self.doc = doc
        self._by_name = {}

    def get(self, page, fontname):
        if fontname in self._by_name:
            return self._by_name[fontname]
        entry = None
        try:
            for xref, ext, _type, basefont, _name, _enc in page.get_fonts():
                if ext == "n/a":
                    continue
                _bn, ext, _ft, buf = self.doc.extract_font(xref)
                if ext == "n/a" or not buf:
                    continue
                font = fitz.Font(fontbuffer=buf)
                names = {basefont.split("+")[-1], font.name, font.name.replace(" ", "")}
                if fontname not in names and fontname.replace("MT", "") not in names:
                    continue
                pools = {
                    "upper": "".join(c for c in _UPPER if font.has_glyph(ord(c))),
                    "lower": "".join(c for c in _LOWER if font.has_glyph(ord(c))),
                    "digits": "".join(c for c in _DIGITS if font.has_glyph(ord(c))),
                }
                entry = {"font": font, "buf": buf, "ref": "RdX%d" % xref, "pools": pools}
                break
        except Exception:
            entry = None
        self._by_name[fontname] = entry
        return entry


# --------------------------------------------------------------------------
# Drawing
# --------------------------------------------------------------------------

def _ink_polygon(rect, pad_x=0.6, pad_y=1.1, jitter=1.4, step=3.2):
    """A rough-edged marker stroke that always fully covers `rect`.
    Edges only ever wobble outward, never inward."""
    x0, y0, x1, y1 = rect.x0 - pad_x, rect.y0 - pad_y, rect.x1 + pad_x, rect.y1 + pad_y
    j = lambda: _rng.uniform(0, jitter)
    pts = []
    n = max(2, int((x1 - x0) / step))
    for i in range(n + 1):  # top edge, left to right
        pts.append(fitz.Point(x0 + (x1 - x0) * i / n, y0 - j()))
    pts.append(fitz.Point(x1 + j() * 0.6, y0 + (y1 - y0) * 0.33))  # ragged right end
    pts.append(fitz.Point(x1 + j() * 0.6, y0 + (y1 - y0) * 0.66))
    for i in range(n, -1, -1):  # bottom edge, right to left
        pts.append(fitz.Point(x0 + (x1 - x0) * i / n, y1 + j()))
    pts.append(fitz.Point(x0 - j() * 0.6, y0 + (y1 - y0) * 0.66))
    pts.append(fitz.Point(x0 - j() * 0.6, y0 + (y1 - y0) * 0.33))
    return pts


def _draw_ink(page, rects):
    if not rects:
        return
    shape = page.new_shape()
    for r in rects:
        if r.height > 22:  # big areas: stacked overlapping strokes, like a felt marker
            strips = math.ceil(r.height / 15.0)
            h = r.height / strips
            for i in range(strips):
                sr = fitz.Rect(r.x0, r.y0 + i * h, r.x1, r.y0 + (i + 1) * h)
                shape.draw_polyline(_ink_polygon(sr))
                shape.finish(fill=_INK, color=None, closePath=True)
        else:
            shape.draw_polyline(_ink_polygon(r))
            shape.finish(fill=_INK, color=None, closePath=True)
    shape.commit(overlay=True)


def _best_fit(word_text, pools, measure, target_width, tries=48):
    """Draw many independent random scrambles and keep the one whose natural
    width is closest to the original slot. The slot width is already visible
    on the page, so choosing by it reveals nothing about the original text."""
    best, best_err = None, None
    for _ in range(tries):
        cand = scramble_text(word_text, pools)
        err = abs(measure(cand) - target_width)
        if best is None or err < best_err:
            best, best_err = cand, err
    return best


def _insert_scrambled(page, word_text, style, target_width, fonts):
    # Soft hyphens extract with zero width but render as hyphens.
    word_text = word_text.replace("\u00ad", "-")
    size = style["size"]
    entry = fonts.get(page, style["font"]) if style["font"] else None
    use_own = entry is not None and all(len(p) >= 5 for p in entry["pools"].values()) \
        and all(entry["font"].has_glyph(ord(c)) for c in word_text if not c.isalnum())
    if use_own:
        try:
            page.insert_font(fontname=entry["ref"], fontbuffer=entry["buf"])
            font = entry["font"]
            new_text = _best_fit(word_text, entry["pools"],
                                 lambda t: font.text_length(t, fontsize=size), target_width)
            fontname = entry["ref"]
            width = font.text_length(new_text, fontsize=size)
        except Exception:
            use_own = False
    if not use_own:
        fontname = _base14(style["font"], style["flags"])
        new_text = _best_fit(word_text, None,
                             lambda t: fitz.get_text_length(t, fontname=fontname, fontsize=size),
                             target_width)
        width = fitz.get_text_length(new_text, fontname=fontname, fontsize=size)

    sx = 1.0
    if width > 0 and target_width > 0:
        sx = max(0.8, min(1.25, target_width / width))
    origin = fitz.Point(style["origin"])
    dx, dy = style["dir"]
    mat = fitz.Matrix(sx, 1)
    if abs(dy) > 1e-3:  # rotated text line
        mat = mat * fitz.Matrix(math.degrees(math.atan2(-dy, dx)))
    page.insert_text(origin, new_text, fontname=fontname, fontsize=size,
                     color=style["color"], morph=(origin, mat))


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------

def _words(page):
    raw = page.get_text("words", sort=False)
    raw.sort(key=lambda w: (w[5], w[6], w[7]))
    return raw


def _styles_for(page, words, indices):
    """Font / size / colour / baseline for selected words, read from the
    page's character data."""
    chars = []
    d = page.get_text("rawdict")
    for block in d["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            for span in line["spans"]:
                for ch in span["chars"]:
                    b = ch["bbox"]
                    chars.append(((b[0] + b[2]) / 2, (b[1] + b[3]) / 2, ch, span, line["dir"]))
    styles = {}
    for i in indices:
        r = fitz.Rect(words[i][:4])
        found = None
        for cx, cy, ch, span, ldir in chars:
            if r.x0 - 0.5 <= cx <= r.x1 + 0.5 and r.y0 - 0.5 <= cy <= r.y1 + 0.5:
                if found is None or (ldir[0] >= 0 and ch["origin"][0] < found[0]["origin"][0]):
                    found = (ch, span, ldir)
        if found is None:
            styles[i] = {"font": "", "flags": 0, "size": max(4.0, r.height * 0.8),
                         "color": (0, 0, 0), "origin": (r.x0, r.y1 - r.height * 0.22),
                         "dir": (1.0, 0.0)}
            continue
        ch, span, ldir = found
        styles[i] = {
            "font": span["font"],
            "flags": span["flags"],
            "size": span["size"],
            "color": fitz.sRGB_to_pdf(span["color"]),
            "origin": tuple(ch["origin"]),
            "dir": tuple(ldir),
        }
    return styles


def _invisible_spans(page):
    """Text drawn in render mode 3 (invisible) -- typically an OCR layer
    over a scanned image. It is extractable but never seen."""
    try:
        return [fitz.Rect(sp["bbox"]) for sp in page.get_texttrace() if sp.get("type") == 3]
    except Exception:
        return []


def _strip_invisible(page):
    rects = [r for r in _invisible_spans(page) if not r.is_empty]
    if not rects:
        return 0
    for r in rects:
        page.add_redact_annot(r, fill=False, cross_out=False)
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                          graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                          text=fitz.PDF_REDACT_TEXT_REMOVE)
    return len(rects)


def _is_scanned(page, has_words, invisible):
    """A page that is essentially one big image (with or without OCR text)."""
    area = abs(page.rect)
    big_image = any(abs(fitz.Rect(i["bbox"]) & page.rect) > 0.6 * area for i in page.get_image_info())
    return big_image and (bool(invisible) or not has_words)


def _pad(rect, frac):
    d = rect.height * frac
    return fitz.Rect(rect.x0 - d, rect.y0 - d * 0.5, rect.x1 + d, rect.y1 + d * 0.5)


def _norm(token):
    return re.sub(r"^[^\w]+|[^\w]+$", "", token).lower()


def _is_sensitive_token(tok):
    return bool(tok) and tok not in _STOPWORDS and (len(tok) >= 3 or any(c.isdigit() for c in tok))


def _group_line_rects(words, indices):
    """Merge neighbouring selected words on the same line into one bar."""
    rects = []
    cur, cur_key, last_i = None, None, None
    for i in sorted(indices):
        w = words[i]
        r = fitz.Rect(w[:4])
        key = (w[5], w[6])
        if cur is not None and key == cur_key and last_i == i - 1 and r.x0 - cur.x1 < r.height * 1.5:
            cur |= r
        else:
            if cur is not None:
                rects.append(cur)
            cur, cur_key = r, key
        last_i = i
    if cur is not None:
        rects.append(cur)
    return rects


# --------------------------------------------------------------------------
# Document session
# --------------------------------------------------------------------------

class RedactionJob:
    """One document being worked on. Holds only in-memory bytes."""

    def __init__(self, data, password=None):
        try:
            doc = fitz.open(stream=data, filetype="pdf")
        except Exception as exc:
            raise NotAPdf(str(exc))
        if not doc.is_pdf:
            raise NotAPdf("not a PDF")
        if doc.needs_pass:
            if not password or not doc.authenticate(password):
                doc.close()
                raise PasswordRequired()
            # Work from a decrypted copy so later re-opens need no password.
            data = doc.tobytes(encryption=fitz.PDF_ENCRYPT_NONE)
            doc.close()
            doc = fitz.open(stream=data, filetype="pdf")
        self._data = data
        self.doc = doc
        self.words = [_words(p) for p in doc]
        self.output = None
        self.output_doc = None

    # ---- what the UI needs -------------------------------------------------

    def page_info(self):
        pages = []
        for pno, page in enumerate(self.doc):
            m = page.rotation_matrix
            words = []
            for w in self.words[pno]:
                r = fitz.Rect(w[:4]) * m
                words.append({"r": [round(r.x0, 2), round(r.y0, 2), round(r.x1, 2), round(r.y1, 2)],
                              "t": w[4], "b": w[5], "l": w[6]})
            images = []
            for info in page.get_image_info():
                r = fitz.Rect(info["bbox"]) * m
                if r.is_empty or r.is_infinite or abs(r & page.rect) > 0.6 * abs(page.rect):
                    continue
                images.append([round(r.x0, 2), round(r.y0, 2), round(r.x1, 2), round(r.y1, 2)])
            pages.append({"w": page.rect.width, "h": page.rect.height,
                          "words": words, "images": images})
        return {"pages": pages, "summary": self._intake_summary()}

    def _intake_summary(self):
        meta = {k: v for k, v in (self.doc.metadata or {}).items() if k in _INFO_KEYS and v}
        n_annots = sum(len(list(p.annots())) for p in self.doc)
        scanned, hidden = [], 0
        for pno, page in enumerate(self.doc):
            inv = _invisible_spans(page)
            hidden += len(inv)
            if _is_scanned(page, bool(self.words[pno]), inv):
                scanned.append(pno + 1)
        return {
            "scanned_pages": scanned,
            "invisible_spans": hidden,
            "page_count": self.doc.page_count,
            "metadata_fields": sorted(meta.keys()),
            "has_xmp": bool(self.doc.get_xml_metadata()),
            "embedded_files": self.doc.embfile_count(),
            "annotations": n_annots,
            "bookmarks": len(self.doc.get_toc()),
            "form_fields": sum(len(list(p.widgets())) for p in self.doc),
            "word_count": sum(len(w) for w in self.words),
        }

    def render(self, pno, zoom=2.0, output=False):
        doc = self.output_doc if output else self.doc
        page = doc[pno]
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), annots=False, alpha=False)
        return pix.tobytes("png")

    # ---- the actual redaction ----------------------------------------------

    def process(self, marks, keep_hidden_text=False, remove_pages=()):
        """marks: list of dicts
             {"page": n, "type": "words", "words": [idx, ...], "method": ...}
             {"page": n, "type": "area",  "rect": [x0,y0,x1,y1], "method": ...}
           Area rects are in displayed (rotated) page coordinates.
           remove_pages: page indices to withdraw from the output entirely."""
        doc = fitz.open(stream=self._data, filetype="pdf")
        fonts = _FontCache(doc)

        removed = {int(p) for p in remove_pages if 0 <= int(p) < doc.page_count}
        keep = [p for p in range(doc.page_count) if p not in removed]
        if not keep:
            doc.close()
            raise ValueError("Every page was withdrawn; at least one must remain.")

        by_page = {}
        for m in marks:
            if int(m["page"]) not in removed:
                by_page.setdefault(int(m["page"]), []).append(m)

        record = {}  # pno -> list of (unrotated rect, method, original text or None)
        self._selected = {}  # pno -> word indices that were struck (incl. inside areas)
        stats = {"blackout_words": 0, "scrambled_words": 0, "areas": 0, "pages_touched": 0,
                 "pages_removed": len(removed)}

        # Strip annotations (comments, stamps, sticky notes...) first: they
        # carry free text the user never saw in the preview.
        for page in doc:
            for xref in [a.xref for a in page.annots()]:
                page.delete_annot(page.load_annot(xref))

        for pno in keep:
            pmarks = by_page.get(pno, [])
            page = doc[pno]
            words = self.words[pno]
            derot = page.derotation_matrix

            black_words, scram_words, areas = set(), set(), []
            for m in pmarks:
                method = SCRAMBLE if m.get("method") == SCRAMBLE else BLACKOUT
                if m.get("type") == "area":
                    r = (fitz.Rect(m["rect"]) * derot).normalize() & page.cropbox
                    if r.is_empty:
                        continue
                    areas.append((r, method))
                    # words sitting inside the area are handled as words too
                    for i, w in enumerate(words):
                        wr = fitz.Rect(w[:4])
                        c = fitz.Point((wr.x0 + wr.x1) / 2, (wr.y0 + wr.y1) / 2)
                        if c in r:
                            (scram_words if method == SCRAMBLE else black_words).add(i)
                else:
                    for i in m.get("words", []):
                        i = int(i)
                        if 0 <= i < len(words):
                            (scram_words if method == SCRAMBLE else black_words).add(i)
            scram_words -= black_words  # if marked both ways, blackout wins

            if not (black_words or scram_words or areas):
                if not keep_hidden_text:
                    _strip_invisible(page)
                continue
            stats["pages_touched"] += 1
            self._selected[pno] = black_words | scram_words
            styles = _styles_for(page, words, scram_words)
            # On scans the OCR boxes rarely sit exactly on the printed glyphs:
            # widen word strikes a little so image pixels at the edges go too.
            pad = 0.2 if _is_scanned(self.doc[pno], True, _invisible_spans(self.doc[pno])) else 0.0

            # Pass 1: areas -- remove text, image pixels and vector art fully inside.
            if areas:
                for r, _method in areas:
                    page.add_redact_annot(r, fill=False, cross_out=False)
                page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS,
                                      graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                                      text=fitz.PDF_REDACT_TEXT_REMOVE)
            # Pass 2: individual words -- keep line art (table rules, underlines).
            wsel = black_words | scram_words
            if wsel:
                for i in wsel:
                    r = fitz.Rect(words[i][:4])
                    page.add_redact_annot(_pad(r, pad) if pad else r,
                                          fill=False, cross_out=False)
                page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS,
                                      graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                                      text=fitz.PDF_REDACT_TEXT_REMOVE)
            # Must happen before scrambled text is drawn, or it would catch that too.
            if not keep_hidden_text:
                _strip_invisible(page)

            # Draw back: ink bars for blackouts...
            bars = [_pad(r, pad) if pad else r for r in _group_line_rects(words, black_words)]
            bars += [r for r, method in areas if method == BLACKOUT]
            # ...and paper-coloured patches where image pixels were wiped under a scramble area
            for r, method in areas:
                if method == SCRAMBLE and any(fitz.Rect(i["bbox"]).intersects(r)
                                              for i in self.doc[pno].get_image_info()):
                    page.draw_rect(r, color=None, fill=(0.82, 0.82, 0.82), overlay=True)
            _draw_ink(page, bars)
            # ...and random replacement characters for scrambles.
            for i in sorted(scram_words):
                wr = fitz.Rect(words[i][:4])
                _insert_scrambled(page, words[i][4], styles[i], wr.width, fonts)

            rec = record.setdefault(pno, [])
            for i in black_words:
                rec.append((fitz.Rect(words[i][:4]), BLACKOUT, words[i][4]))
            for i in scram_words:
                rec.append((fitz.Rect(words[i][:4]), SCRAMBLE, words[i][4]))
            stats["blackout_words"] += len(black_words)
            stats["scrambled_words"] += len(scram_words)
            stats["areas"] += len(areas)

        # Withdrawn pages: dropped from the page tree here; garbage collection
        # on save then discards their content streams and any images, fonts or
        # other objects that nothing else uses.
        if removed:
            doc.select(keep)

        # Document-wide sanitation.
        doc.scrub(
            attached_files=True, clean_pages=True, embedded_files=True,
            hidden_text=False, javascript=True, metadata=True,  # hidden text: done above
            redactions=True, redact_images=fitz.PDF_REDACT_IMAGE_NONE,
            remove_links=True, reset_fields=True, reset_responses=True,
            thumbnails=True, xml_metadata=True,
        )
        doc.set_metadata({})
        doc.del_xml_metadata()
        doc.set_toc([])
        cat = doc.pdf_catalog()
        if doc.xref_get_key(cat, "Names/EmbeddedFiles")[0] != "null":
            doc.xref_set_key(cat, "Names/EmbeddedFiles", "null")
        if doc.xref_get_key(cat, "PageMode")[1] in ("/UseAttachments", "/UseOutlines"):
            doc.xref_set_key(cat, "PageMode", "/UseNone")
        # Drop the now-null entries outright rather than leaving stubs behind.
        obj = doc.xref_object(cat, compressed=True)
        obj = re.sub(r"/(EmbeddedFiles|Outlines) null", "", obj)
        obj = re.sub(r"/Names\s*<<\s*>>", "", obj)
        doc.update_object(cat, obj)
        try:
            # Re-subset fonts so glyphs only used by redacted text are dropped.
            doc.subset_fonts()
        except Exception:
            pass

        out = doc.tobytes(garbage=4, deflate=True, clean=True, deflate_images=True,
                          deflate_fonts=True, encryption=fitz.PDF_ENCRYPT_NONE)
        doc.close()

        if self.output_doc is not None:
            self.output_doc.close()
        self.output = out
        self.output_doc = fitz.open(stream=out, filetype="pdf")
        report = self._verify(record, keep, keep_hidden_text)
        report["stats"] = stats
        report["size_bytes"] = len(out)
        return report

    # ---- verification ------------------------------------------------------

    def _verify(self, record, keep, keep_hidden_text=False):
        out = self.output_doc
        checks = []
        out_index = {orig: i for i, orig in enumerate(keep)}
        removed = [p for p in range(len(self.words)) if p not in out_index]

        # 1. Every redacted word is gone from the text layer at its location.
        failures = []
        for pno, items in record.items():
            page = out[out_index[pno]]
            for rect, method, original in items:
                probe = fitz.Rect(rect.x0 + rect.width * 0.2, rect.y0 + rect.height * 0.25,
                                  rect.x1 - rect.width * 0.2, rect.y1 - rect.height * 0.25)
                got = page.get_textbox(probe).strip() if not probe.is_empty else ""
                if method == BLACKOUT and got and _norm(got) and _norm(got) in _norm(original):
                    failures.append({"page": pno + 1, "text": got})
                if method == SCRAMBLE and len(original) >= 3 and _norm(got) == _norm(original):
                    failures.append({"page": pno + 1, "text": got})
        total = sum(len(v) for v in record.values())
        checks.append({
            "label": "Marked text purged from text layer",
            "ok": not failures,
            "detail": ("%d marked words verified gone" % total) if not failures
            else "%d marked words still readable" % len(failures),
        })

        # 1a. Withdrawn pages.
        if removed:
            checks.append(self._check_withdrawn(out, keep, removed))

        # 1b. Invisible (OCR) text.
        n_hidden = sum(len(_invisible_spans(p)) for p in out)
        if keep_hidden_text:
            checks.append({"label": "Invisible OCR text retained (by request)", "ok": True,
                           "detail": "%d invisible span(s) kept" % n_hidden})
        else:
            checks.append({"label": "Invisible text layer stripped", "ok": n_hidden == 0,
                           "detail": "none remaining" if not n_hidden
                           else "%d invisible span(s) remain" % n_hidden})

        # 2. Document metadata.
        meta = {k: v for k, v in (out.metadata or {}).items() if k in _INFO_KEYS and v}
        checks.append({"label": "Document info dictionary wiped", "ok": not meta,
                       "detail": "empty" if not meta else ", ".join(sorted(meta))})
        xmp = out.get_xml_metadata()
        checks.append({"label": "XMP metadata destroyed", "ok": not xmp,
                       "detail": "none present" if not xmp else "%d bytes remain" % len(xmp)})

        # 3. Annotations, bookmarks, attachments.
        n_annots = sum(len(list(p.annots())) for p in out)
        checks.append({"label": "Comments & annotations removed", "ok": n_annots == 0,
                       "detail": "%d remaining" % n_annots})
        toc = out.get_toc()
        checks.append({"label": "Bookmarks / outline removed", "ok": not toc,
                       "detail": "%d remaining" % len(toc)})
        checks.append({"label": "Embedded files removed", "ok": out.embfile_count() == 0,
                       "detail": "%d remaining" % out.embfile_count()})

        # 4. Raw object scan for active content and leftovers.
        flagged = set()
        for xref in range(1, out.xref_length()):
            try:
                obj = out.xref_object(xref, compressed=False)
            except Exception:
                continue
            for key, label in (("/JavaScript", "JavaScript"), ("/JS", "JavaScript"),
                               ("/Type /EmbeddedFile", "embedded file"), ("/EmbeddedFiles", "attachment list"), ("/Launch", "launch action"),
                               ("/URI", "link"), ("/Subtype /Redact", "unapplied redaction"),
                               ("/Popup", "popup note")):
                if key in obj:
                    flagged.add(label)
        checks.append({"label": "No scripts, links or hidden payloads", "ok": not flagged,
                       "detail": "raw object scan clean" if not flagged else ", ".join(sorted(flagged))})

        # 5. Leaks: terms the user redacted that still appear elsewhere.
        terms = {}
        for pno, items in record.items():
            for _rect, _method, original in items:
                t = _norm(original)
                if _is_sensitive_token(t):
                    terms.setdefault(t, original)
        selected = getattr(self, "_selected", {})
        out_tokens = {}  # token -> original page numbers it still appears on
        for i, page in enumerate(out):
            for w in page.get_text("words"):
                out_tokens.setdefault(_norm(w[4]), set()).add(keep[i])

        def hits(term, token):
            # whole word, or embedded in a longer token (emails, URLs, usernames)
            return token == term or (len(term) >= 4 and term in token)

        leaks = []
        for t, shown in sorted(terms.items()):
            pages_hit = {pno for tok, pnos in out_tokens.items() if hits(t, tok) for pno in pnos}
            occ = []
            for pno, words in enumerate(self.words):
                if pno not in out_index:
                    continue
                # unstruck occurrences in the original -- this also catches words
                # that are still visible in a scanned image after its OCR layer
                # was stripped from the text
                idx = [i for i, w in enumerate(words) if hits(t, _norm(w[4]))
                       and i not in selected.get(pno, set())]
                if idx:
                    occ.append({"page": pno, "words": idx})
                    pages_hit.add(pno)
            if pages_hit:
                leaks.append({"term": shown, "pages": sorted(p + 1 for p in pages_hit),
                              "occurrences": occ})
        checks.append({"label": "Redacted terms absent elsewhere", "ok": not leaks,
                       "detail": "no repeats found" if not leaks
                       else "%d term(s) still appear unredacted" % len(leaks),
                       "warn_only": True})

        text_layer = [p.get_text("text") for p in out]
        return {"checks": checks, "leaks": leaks, "text_layer": text_layer,
                "page_count": out.page_count, "kept_pages": keep}

    def _check_withdrawn(self, out, keep, removed):
        """Withdrawn pages must leave nothing behind: not their text, and not
        any image that only they used."""
        problems = []
        if out.page_count != len(keep):
            problems.append("page count is %d, expected %d" % (out.page_count, len(keep)))

        kept_tokens = {_norm(w[4]) for p in keep for w in self.words[p]}
        exclusive = {_norm(w[4]) for p in removed for w in self.words[p]} - kept_tokens
        exclusive = {t for t in exclusive if _is_sensitive_token(t)}
        out_tokens = {_norm(w[4]) for page in out for w in page.get_text("words")}
        survived = exclusive & out_tokens
        if survived:
            problems.append("%d word(s) from withdrawn pages still present" % len(survived))

        def image_digests(doc, pages):
            found = set()
            for p in pages:
                for info in doc[p].get_image_info(hashes=True):
                    if info.get("digest"):
                        found.add(info["digest"])
            return found

        def all_image_digests(doc):
            # every image object in the file, referenced by a page or not
            found = set()
            for xref in range(1, doc.xref_length()):
                try:
                    if doc.xref_get_key(xref, "Subtype")[1] != "/Image":
                        continue
                    pix = fitz.Pixmap(doc, xref)
                    found.add(fitz.Pixmap(pix, 0).digest if pix.alpha else pix.digest)
                except Exception:
                    continue
            return found

        try:
            only_removed = image_digests(self.doc, removed) - image_digests(self.doc, keep)
            if only_removed & all_image_digests(out):
                problems.append("image(s) from withdrawn pages still embedded")
        except Exception:
            pass

        pages = ", ".join(str(p + 1) for p in removed)
        return {"label": "Withdrawn pages destroyed", "ok": not problems,
                "detail": "page(s) %s gone, %d word(s) and their images verified absent"
                          % (pages, len(exclusive)) if not problems else "; ".join(problems)}

    def close(self):
        for d in (self.doc, self.output_doc):
            if d is not None:
                d.close()
        self._data = None
        self.output = None
        self.words = None
        self.doc = None
        self.output_doc = None
