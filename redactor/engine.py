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

import pymupdf as fitz  # PyMuPDF

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


def _can_scramble(text):
    """Whether scramble_text would replace anything in `text`. It keeps everything else,
    so a word of only punctuation comes back as it was."""
    return any(ch.isdigit() or ch.isalpha() for ch in text)


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
    on the page, so choosing by it reveals nothing about the original text.

    A draw of the original is thrown away, because it fits its own slot exactly and would
    almost always win. Each letter stays independent, so the result can still share letters
    with the original. Only a word with nothing to scramble comes back unchanged. The loop
    ends because every pool has at least 5 characters (_insert_scrambled checks this)."""
    if not _can_scramble(word_text):
        return word_text
    original = _norm(word_text)
    best, best_err = None, None
    draws = 0
    while draws < tries or best is None:
        draws += 1
        cand = scramble_text(word_text, pools)
        if _norm(cand) == original:
            continue
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
# Text that is in the file but can't be seen
# --------------------------------------------------------------------------

_TINY_TEXT_PT = 1.5     # nobody reads this size: microscopic text is a hiding place
_VISIBLE_DELTA = 10     # grey levels of change a span must cause to count as visible
_EVERYWHERE = fitz.Rect(-1e5, -1e5, 1e5, 1e5)


def _span_is_blank(span):
    return not "".join(chr(c[0]) for c in span.get("chars", ()) if c[0] > 0).strip()


def _strike_rect(span):
    """The span's box, pulled in so a redaction can't clip neighbouring text."""
    r = fitz.Rect(span["bbox"])
    chars = span.get("chars") or ()
    left = fitz.Rect(chars[0][3]).width if chars else 0
    right = fitz.Rect(chars[-1][3]).width if chars else 0
    return fitz.Rect(r.x0 + left * 0.25, r.y0 + r.height * 0.25,
                     r.x1 - right * 0.25, r.y1 - r.height * 0.25)


def _pixels_differ(a, b, box):
    """Does `box` (pixel coordinates) differ between two same-sized grey pixmaps?"""
    x0, y0 = max(0, int(box.x0)), max(0, int(box.y0))
    x1, y1 = min(a.width, math.ceil(box.x1)), min(a.height, math.ceil(box.y1))
    if x1 <= x0 or y1 <= y0:
        return True  # nothing on the page to compare: off-page text is handled separately
    sa, sb, stride = a.samples_mv, b.samples_mv, a.stride
    for y in range(y0, y1):
        ra, rb = sa[y * stride + x0:y * stride + x1], sb[y * stride + x0:y * stride + x1]
        if ra != rb and any(abs(p - q) >= _VISIBLE_DELTA for p, q in zip(ra, rb)):
            return True
    return False


def _drop_annotations(doc):
    """Comments, stamps, notes and form fields all live in /Annots. Clearing it before any
    page is loaded means no annotation or widget objects to outlive the document, which
    has crashed PyMuPDF. Call this straight after opening."""
    for pno in range(doc.page_count):
        doc.xref_set_key(doc.page_xref(pno), "Annots", "null")


def _concealed_text(pdf_bytes):
    """Find text a reader can't see but a text extractor can: under a drawn box or an
    image, white on white, fully transparent, or microscopic. For each page returns the
    rects to strike, in the same coordinates as words.

    Whether a span shows is measured, not guessed from its colour: the page is rendered
    with and without its text, and a span that changes no pixels is concealed. A hidden
    span overlapping visible text changes pixels, so it is left alone rather than risk
    deleting something the reader can see."""
    work = fitz.open(stream=pdf_bytes, filetype="pdf")
    found = {}
    try:
        # Field and annotation appearances show up in the trace but are removed separately.
        _drop_annotations(work)
        for pno in range(work.page_count):
            page = work[pno]
            spans = [s for s in page.get_texttrace()
                     if s.get("type") != 3 and not _span_is_blank(s)]
            if not spans:
                continue
            hits = [s for s in spans if s["size"] < _TINY_TEXT_PT]
            rest = [s for s in spans if s["size"] >= _TINY_TEXT_PT]
            if rest:
                zoom = 1.5
                mat = fitz.Matrix(zoom, zoom)
                full = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY, annots=False, alpha=False)
                rotate = page.rotation_matrix * mat
                page.add_redact_annot(_EVERYWHERE, fill=False, cross_out=False)
                page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                                      graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                                      text=fitz.PDF_REDACT_TEXT_REMOVE)
                # If some text couldn't be lifted the comparison proves nothing.
                if not page.get_texttrace():
                    bare = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY, annots=False, alpha=False)
                    hits += [s for s in rest
                             if not _pixels_differ(full, bare, (fitz.Rect(s["bbox"]) * rotate).normalize())]
            if hits:
                found[pno] = [fitz.Rect(s["bbox"]) for s in hits]
    finally:
        work.close()
    return found


def _strike_concealed(page, rects):
    """Lift concealed text out of the page. Rects come from _concealed_text()."""
    wanted = [fitz.Rect(r) for r in rects]
    strikes = [_strike_rect(s) for s in page.get_texttrace()
               if any(fitz.Rect(s["bbox"]).intersects(r) for r in wanted)]
    strikes = [r for r in strikes if not r.is_empty]
    if not strikes:
        return
    for r in strikes:
        page.add_redact_annot(r, fill=False, cross_out=False)
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                          graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                          text=fitz.PDF_REDACT_TEXT_REMOVE)


def _visible_box(page):
    """The part of the page a reader sees, in the coordinates redactions use."""
    return (page.rect * page.derotation_matrix).normalize()


def _past_the_edge(page):
    """Is anything drawn outside the visible page (including outside a crop)?"""
    box = _visible_box(page) + (-1, -1, 1, 1)
    try:
        log = page.get_bboxlog()
    except Exception:
        return True
    return any(not fitz.Rect(item[1]).is_empty and not box.contains(fitz.Rect(item[1])) for item in log) \
        or any(not box.contains(fitz.Rect(s["bbox"])) for s in page.get_texttrace() if not _span_is_blank(s))


def _strip_past_the_edge(page):
    """Off-page text, images and art print nowhere but stay in the file. Page crops
    work the same way, so this also removes what a crop was hiding."""
    if not _past_the_edge(page):
        return
    box, far = _visible_box(page), 1e5
    for r in (fitz.Rect(-far, -far, far, box.y0), fitz.Rect(-far, box.y1, far, far),
              fitz.Rect(-far, box.y0, box.x0, box.y1), fitz.Rect(box.x1, box.y0, far, box.y1)):
        page.add_redact_annot(r, fill=False, cross_out=False)
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS,
                          graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                          text=fitz.PDF_REDACT_TEXT_REMOVE)


def _wipe_image_masks(doc, page, rects):
    """MuPDF wipes an image's own pixels under a mark but not its transparency mask, so a
    shape stored in the mask would survive. Each image hit gets a private, wiped copy."""
    rects = [r for r in rects if not r.is_empty]
    if not rects:
        return
    for item in page.get_images(full=True):
        xref, smask = item[0], item[1]
        stencil = doc.xref_get_key(xref, "Mask")
        if not smask and stencil[0] != "xref":
            continue
        try:
            placed = page.get_image_rects(xref, transform=True)
        except Exception:
            placed = []
        hit = [(r, m) for bbox, m in placed for r in rects if bbox.intersects(r)]
        if not hit:
            continue
        pix = None
        if smask:
            try:
                pix = fitz.Pixmap(doc, smask)
                pix = pix if pix.n == 1 and not pix.alpha else None
            except Exception:
                pix = None
        if pix is None:
            # A stencil or unreadable mask: replace it with a fully transparent one.
            pix = fitz.Pixmap(fitz.csGRAY, 1, 1, b"\0", False)
            region = [pix.irect]
        else:
            region = []
            for r, m in hit:
                # image space is the unit square; a singular matrix means "wipe all"
                unit = (r * ~m).normalize() if m.is_rectilinear or abs(m.a * m.d - m.b * m.c) > 1e-9 else None
                if unit is None:
                    region = [pix.irect]
                    break
                box = fitz.Rect(unit.x0 * pix.width - 1, unit.y0 * pix.height - 1,
                                unit.x1 * pix.width + 1, unit.y1 * pix.height + 1).irect & pix.irect
                if not box.is_empty:
                    region.append(box)
        for box in region:
            pix.set_rect(box, (0,))
        fresh = doc.get_new_xref()
        doc.update_object(fresh, "<< /Type /XObject /Subtype /Image /Width %d /Height %d "
                                 "/ColorSpace /DeviceGray /BitsPerComponent 8 >>" % (pix.width, pix.height))
        doc.update_stream(fresh, bytes(pix.samples), new=True)
        doc.xref_set_key(xref, "SMask", "%d 0 R" % fresh)
        _drop_keys(doc, xref, [k for k in ("Mask", "Matte") if doc.xref_get_key(xref, k)[0] != "null"])


# --------------------------------------------------------------------------
# Content streams: hidden layers and marked content
# --------------------------------------------------------------------------

_WS = frozenset(b"\x00\t\n\x0c\r ")
_NOT_REGULAR = _WS | frozenset(b"()<>[]{}/%")
_NUMBER = re.compile(rb"^[+-]?(?:\d+\.?\d*|\.\d+)$")
_INLINE_END = re.compile(rb"[\x00\t\n\x0c\r ]EI(?=[\x00\t\n\x0c\r ]|$)")
_SHOW_TEXT = {b"Tj", b"TJ"}
_MOVE_AND_SHOW = {b"'", b'"'}
_PAINT_PATH = {b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*", b"S", b"s"}
_MARKED_OPS = re.compile(rb"(?<![^\x00\t\n\x0c\r )>\]])(?:BDC|BMC|EMC|MP|DP)(?![^\x00\t\n\x0c\r (</\[])")


def _skip_ws(data, i):
    n = len(data)
    while i < n:
        if data[i] in _WS:
            i += 1
        elif data[i] == 0x25:  # comment
            while i < n and data[i] not in b"\r\n":
                i += 1
        else:
            break
    return i


def _end_of_regular(data, i):
    n = len(data)
    while i < n and data[i] not in _NOT_REGULAR:
        i += 1
    return i


def _end_of_string(data, i):
    depth, n = 0, len(data)
    while i < n:
        c = data[i]
        if c == 0x5c:
            i += 2
            continue
        if c == 0x28:
            depth += 1
        elif c == 0x29:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError("unterminated string")


def _end_of_hex(data, i):
    end = data.find(b">", i)
    if end < 0:
        raise ValueError("unterminated hex string")
    return end + 1


def _end_of_composite(data, i):
    """End of the [array] or <<dict>> at i. Iterative, so hostile nesting can't overflow."""
    depth, n = 0, len(data)
    while i < n:
        i = _skip_ws(data, i)
        if i >= n:
            break
        c, pair = data[i], data[i:i + 2]
        if pair == b"<<" or c == 0x5b:
            depth += 1
            i += 2 if pair == b"<<" else 1
        elif pair == b">>" or c == 0x5d:
            depth -= 1
            i += 2 if pair == b">>" else 1
            if depth == 0:
                return i
        elif c == 0x28:
            i = _end_of_string(data, i)
        elif c == 0x3c:
            i = _end_of_hex(data, i)
        elif c == 0x2f:
            i = _end_of_regular(data, i + 1)
        else:
            i = max(_end_of_regular(data, i), i + 1)
    raise ValueError("unterminated array or dictionary")


def _instructions(data):
    """Split a content stream into (operator, operands, raw bytes). Raises ValueError on
    anything it can't parse: callers must treat that as a failure, not skip the stream."""
    i, n, start, operands = 0, len(data), None, []
    while True:
        i = _skip_ws(data, i)
        if i >= n:
            break
        if start is None:
            start = i
        c, pair = data[i], data[i:i + 2]
        if c == 0x28:
            end = _end_of_string(data, i)
        elif pair == b"<<" or c == 0x5b:
            end = _end_of_composite(data, i)
        elif c == 0x3c:
            end = _end_of_hex(data, i)
        elif c == 0x2f:
            end = _end_of_regular(data, i + 1)
        elif c in _NOT_REGULAR:
            raise ValueError("unexpected %r in content stream" % bytes((c,)))
        else:
            end = _end_of_regular(data, i)
            word = data[i:end]
            if not (_NUMBER.match(word) or word in (b"true", b"false", b"null")):
                if word == b"ID":  # inline image: raw bytes up to EI
                    m = _INLINE_END.search(data, end)
                    if not m:
                        raise ValueError("unterminated inline image")
                    end = m.end()
                yield word, operands, data[start:end]
                i, start, operands = end, None, []
                continue
        operands.append(data[i:end])
        i = end
    if operands:
        yield None, operands, data[start:]


def _flatten_marked_content(data, oc_hidden, xobject_hidden):
    """Rewrite a content stream with marked content gone. That takes /ActualText, /Alt and
    similar text out of the stream, and content on a hidden layer with it. Graphics state
    inside a hidden section is kept, because visible content after it may depend on it."""
    out, stack, hidden = [], [], 0
    for op, operands, raw in _instructions(data):
        if op == b"BMC":
            stack.append(False)
        elif op == b"BDC":
            is_hidden = len(operands) >= 2 and operands[0] == b"/OC" and oc_hidden(operands[1])
            stack.append(is_hidden)
            hidden += is_hidden
        elif op == b"EMC":
            if stack:
                hidden -= stack.pop()
        elif op in (b"MP", b"DP"):
            pass
        elif op == b"Do" and (hidden or (operands and xobject_hidden(operands[-1]))):
            pass
        elif hidden and op in (_SHOW_TEXT | {b"sh", b"BI", b"ID"}):
            pass
        elif hidden and op in _MOVE_AND_SHOW:
            out.append(b"T*")
        elif hidden and op in _PAINT_PATH:
            out.append(b"n")
        else:
            out.append(raw)
    return b"\n".join(out)


class _Layers:
    """Which optional-content groups (layers) are off in the document's default view."""

    def __init__(self, doc):
        self.doc = doc
        self.ocgs = doc.get_ocgs() or {}
        self.cache = {}

    def _ve(self, node):
        if isinstance(node, int):
            return self.ocgs.get(node, {"on": True})["on"]
        if not node:
            return True
        op, rest = node[0], node[1:]
        if op == "not":
            return not self._ve(rest[0])
        results = [self._ve(x) for x in rest]
        return all(results) if op == "and" else any(results)

    def _visible(self, xref):
        if xref in self.ocgs:
            return self.ocgs[xref]["on"]
        if self.doc.xref_get_key(xref, "Type")[1] == "/OCG":
            return True  # not listed in /OCProperties: viewers show it
        try:
            md = self.doc.get_ocmd(xref)
        except Exception:
            return False  # can't tell what controls it: don't keep it
        if md.get("ve"):
            return self._ve(md["ve"])
        states = [self.ocgs.get(x, {"on": True})["on"] for x in md.get("ocgs") or []]
        if not states:
            return True
        return {"AllOn": all(states), "AnyOff": not all(states),
                "AllOff": not any(states)}.get(md.get("policy"), any(states))

    def hidden(self, xref):
        if xref not in self.cache:
            self.cache[xref] = not self._visible(xref)
        return self.cache[xref]


def _resource_xref(doc, owner, kind, name):
    """Follow Resources/<kind>/<name> from a page or form, including page-tree inheritance."""
    name = re.sub(r"#([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), name)
    for _ in range(64):
        typ, val = doc.xref_get_key(owner, "Resources/%s/%s" % (kind, name))
        if typ == "xref":
            return int(val.split()[0])
        if doc.xref_get_key(owner, "Resources")[0] != "null":
            return None
        parent = doc.xref_get_key(owner, "Parent")
        if parent[0] != "xref":
            return None
        owner = int(parent[1].split()[0])
    return None


def _flatten_stream(doc, owner, data, layers):
    def oc_hidden(operand):
        if not operand.startswith(b"/"):
            return True  # OC must name a resource: anything else can't be classified
        xref = _resource_xref(doc, owner, "Properties", operand[1:].decode("latin-1"))
        return xref is not None and layers.hidden(xref)

    def xobject_hidden(operand):
        if not layers.ocgs or not operand.startswith(b"/"):
            return False
        xref = _resource_xref(doc, owner, "XObject", operand[1:].decode("latin-1"))
        if xref is None:
            return False
        typ, val = doc.xref_get_key(xref, "OC")
        return typ == "xref" and layers.hidden(int(val.split()[0]))

    return _flatten_marked_content(data, oc_hidden, xobject_hidden)


def _flatten_document(doc):
    """Hidden-layer and marked content out of every page, form and tiling pattern.
    Returns how many streams couldn't be parsed: their contents can't be vouched for."""
    layers, failures = _Layers(doc), 0
    for page in doc:
        data = page.read_contents()
        if not data:
            continue
        try:
            new = _flatten_stream(doc, page.xref, data, layers)
        except ValueError:
            failures += 1
            continue
        # A new stream: the old ones may be shared with other pages.
        fresh = doc.get_new_xref()
        doc.update_object(fresh, "<<>>")
        doc.update_stream(fresh, new, new=True)
        page.set_contents(fresh)
    for xref in range(1, doc.xref_length()):
        if not doc.xref_is_stream(xref):
            continue
        if doc.xref_get_key(xref, "Subtype")[1] != "/Form" and doc.xref_get_key(xref, "PatternType")[1] != "1":
            continue
        data = doc.xref_stream(xref) or b""
        try:
            new = _flatten_stream(doc, xref, data, layers)
        except ValueError:
            failures += 1
            continue
        if new != data:
            doc.update_stream(xref, new)
    return failures


# --------------------------------------------------------------------------
# File structure: keep only what rendering needs
# --------------------------------------------------------------------------

# Allowlists rather than a list of known-bad keys: a PDF can carry private data under any
# key at all, and a key nobody has heard of is precisely the one a blocklist would miss.
_KEEP_CATALOG = {"Type", "Pages", "Version"}
_KEEP_PAGE = {"Type", "Parent", "Resources", "MediaBox", "CropBox", "Rotate", "Contents", "UserUnit", "Group"}
_KEEP_PAGES = {"Type", "Parent", "Kids", "Count", "Resources", "MediaBox", "CropBox", "Rotate"}
_KEEP_FORM = {"Type", "Subtype", "FormType", "BBox", "Matrix", "Resources", "Group",
              "Length", "Filter", "DecodeParms"}
_KEEP_IMAGE = {"Type", "Subtype", "Width", "Height", "ColorSpace", "BitsPerComponent", "Intent",
               "ImageMask", "Mask", "Decode", "Interpolate", "SMask", "SMaskInData", "Matte",
               "Length", "Filter", "DecodeParms"}
# Anywhere else, these never affect how a page looks and are known to hold text.
_DROP_ANYWHERE = {"PieceInfo", "Metadata", "LastModified", "AF", "AA", "OC", "OPI", "Alternates", "Ref",
                  "Thumb", "StructParent", "StructParents", "Properties"}
_FORBIDDEN = re.compile(r"(?<![A-Za-z0-9#])/(StructTreeRoot|StructElem|MarkInfo|StructParents?|ActualText|Alt|"
                        r"PieceInfo|OCProperties|OCGs?|OCMD|OC|AcroForm|Widget|DV|TU|PageLabels|Dests|OPI|"
                        r"Alternates|Metadata|LastModified|Thumb|AF|AA|OpenAction|Properties)(?![A-Za-z0-9])")


def _drop_keys(doc, xref, paths):
    """Remove dictionary entries outright: setting one to null leaves a /Key null stub."""
    for path in paths:
        doc.xref_set_key(xref, path, "null")
    obj = doc.xref_object(xref, compressed=True)
    for path in paths:
        obj = re.sub(r"/%s\s+null(?![A-Za-z0-9])" % re.escape(path.split("/")[-1]), "", obj)
    doc.update_object(xref, obj)


def _keep_only(doc, xref, keep):
    drop = [key for key in doc.xref_get_keys(xref) if key not in keep]
    if drop:
        _drop_keys(doc, xref, drop)


def _strip_structure(doc):
    """Reduce the catalog, page tree and XObjects to what rendering needs. This also cuts
    the references (structure elements, form fields, named destinations) that would
    otherwise keep a withdrawn page's objects in the file through garbage collection."""
    catalog = doc.pdf_catalog()
    _keep_only(doc, catalog, _KEEP_CATALOG)
    pages = {doc.page_xref(i) for i in range(doc.page_count)}
    for xref in range(1, doc.xref_length()):
        try:
            keys = doc.xref_get_keys(xref)
        except Exception:
            continue
        if not keys or xref == catalog:
            continue
        typ, subtype = doc.xref_get_key(xref, "Type")[1], doc.xref_get_key(xref, "Subtype")[1]
        if xref in pages:
            _keep_only(doc, xref, _KEEP_PAGE)
        elif typ == "/Pages":
            _keep_only(doc, xref, _KEEP_PAGES)
        elif subtype == "/Form" and doc.xref_is_stream(xref):
            _keep_only(doc, xref, _KEEP_FORM)
        elif subtype == "/Image" and doc.xref_is_stream(xref):
            _keep_only(doc, xref, _KEEP_IMAGE)
        else:
            _keep_only(doc, xref, set(keys) - _DROP_ANYWHERE)
        # An inline Resources dictionary. An indirect one is an object of its own, which
        # _DROP_ANYWHERE has already handled, and a path can't cross a reference anyway.
        if doc.xref_get_key(xref, "Resources")[0] == "dict" \
                and doc.xref_get_key(xref, "Resources/Properties")[0] != "null":
            _drop_keys(doc, xref, ["Resources/Properties"])


def _jpeg_without_metadata(data):
    """Drop JPEG segments that only carry metadata (EXIF, GPS, XMP, comments) without
    re-encoding. APP0 (JFIF), APP2 (ICC) and APP14 (Adobe colour) affect how it decodes."""
    if data[:2] != b"\xff\xd8":
        return None
    out, i, n, changed = [data[:2]], 2, len(data), False
    while i + 4 <= n and data[i] == 0xFF:
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker == 0xDA:  # start of scan: everything after this is image data
            break
        size = int.from_bytes(data[i + 2:i + 4], "big")
        if size < 2 or i + 2 + size > n:
            return None
        if (0xE0 <= marker <= 0xEF and marker not in (0xE0, 0xE2, 0xEE)) or marker == 0xFE:
            changed = True
        else:
            out.append(data[i:i + 2 + size])
        i += 2 + size
    out.append(data[i:])
    return b"".join(out) if changed else None


def _strip_jpeg_metadata(doc):
    for xref in range(1, doc.xref_length()):
        if not doc.xref_is_stream(xref) or doc.xref_get_key(xref, "Subtype")[1] != "/Image":
            continue
        if doc.xref_get_key(xref, "Filter")[1] != "/DCTDecode":
            continue
        clean = _jpeg_without_metadata(doc.xref_stream_raw(xref) or b"")
        if clean is not None:
            doc.update_stream(xref, clean, compress=False)
            doc.xref_set_key(xref, "Filter", "/DCTDecode")


def _orphan_pages(doc):
    """Page objects still in the file but not in the page tree: recoverable pages."""
    live = {doc.page_xref(i) for i in range(doc.page_count)}
    return [x for x in range(1, doc.xref_length())
            if x not in live and doc.xref_get_key(x, "Type")[1] == "/Page"]


def _leftover_structures(doc):
    """Names of hidden-data carriers still anywhere in the file."""
    found = set()
    catalog = doc.pdf_catalog()
    found.update(k for k in doc.xref_get_keys(catalog) if k not in _KEEP_CATALOG)
    for xref in range(1, doc.xref_length()):
        try:
            obj = doc.xref_object(xref, compressed=True)
        except Exception:
            continue
        if "/Differences" in obj:  # glyph names, which can be anything
            continue
        found.update(_FORBIDDEN.findall(obj))
    return found


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
        self.concealed = _concealed_text(data)
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
            "concealed_text": sum(len(v) for v in self.concealed.values()),
            "hidden_layers": sum(1 for g in (self.doc.get_ocgs() or {}).values() if not g["on"]),
            "tagged": self.doc.xref_get_key(self.doc.pdf_catalog(), "StructTreeRoot")[0] != "null",
            "past_the_edge": sum(1 for p in self.doc if _past_the_edge(p)),
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

        # Strip annotations (comments, stamps, sticky notes...) and form fields first:
        # they carry free text the user never saw in the preview. Field names, tooltips
        # and default values are all readable, so resetting a field isn't enough.
        _drop_annotations(doc)

        for pno in keep:
            page = doc[pno]
            if self.concealed.get(pno):
                _strike_concealed(page, self.concealed[pno])
            _strip_past_the_edge(page)

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
            _wipe_image_masks(doc, page, [r for r, _m in areas] +
                              [_pad(fitz.Rect(words[i][:4]), pad) if pad else fitz.Rect(words[i][:4])
                               for i in wsel])
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

        # Withdrawn pages: dropped from the page tree here. Garbage collection on save
        # then discards their content streams and any images, fonts or other objects
        # that nothing else uses, once _strip_structure has cut the other references.
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
        unparsed = _flatten_document(doc)  # needs layer info, so before the catalog goes
        _strip_structure(doc)
        _strip_jpeg_metadata(doc)
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
        report = self._verify(record, keep, keep_hidden_text, unparsed)
        report["stats"] = stats
        report["size_bytes"] = len(out)
        return report

    # ---- verification ------------------------------------------------------

    def _check_hidden_data(self, out, record, unparsed=0):
        """Re-inspect the finished file for anything a reader can't see."""
        problems = []
        orphans = _orphan_pages(out)
        if orphans:
            problems.append("%d page object(s) left outside the page tree" % len(orphans))
        leftover = _leftover_structures(out)
        if leftover:
            problems.append("hidden structures remain: " + ", ".join(sorted(leftover)))
        if unparsed:
            problems.append("%d content stream(s) could not be read and may hide content" % unparsed)
        for page in out:
            try:
                data = page.read_contents() or b""
                if any(op in (b"BDC", b"BMC", b"MP", b"DP") for op, _o, _r in _instructions(data)):
                    problems.append("marked content remains on page %d" % (page.number + 1))
                    break
            except ValueError:
                problems.append("content stream on page %d could not be read" % (page.number + 1))
                break
        concealed, past = 0, 0
        marked = {}  # a mark's bar or scramble may legitimately sit over or be the text
        for pno, items in record.items():
            marked[pno] = [r for r, _m, _o in items]
        found = _concealed_text(self.output)
        for i, rects in found.items():
            orig = self._kept[i]
            near = [m for m in marked.get(orig, [])]
            concealed += sum(1 for r in rects if not any(r.intersects(m) for m in near))
        for page in out:
            box = _visible_box(page) + (-1, -1, 1, 1)
            past += sum(1 for s in page.get_texttrace()
                        if not _span_is_blank(s) and not box.contains(fitz.Rect(s["bbox"])))
        if concealed:
            problems.append("%d run(s) of concealed text remain" % concealed)
        if past:
            problems.append("%d run(s) of text remain outside the page" % past)
        return {"label": "No concealed text, hidden layers or private data", "ok": not problems,
                "detail": "structure, layers and text visibility verified" if not problems
                else "; ".join(problems)}

    def _verify(self, record, keep, keep_hidden_text=False, unparsed=0):
        out = self.output_doc
        self._kept = keep
        checks = []
        out_index = {orig: i for i, orig in enumerate(keep)}
        removed = [p for p in range(len(self.words)) if p not in out_index]

        # 1. Every redacted word is gone from the text layer at its location.
        failures = []
        for pno, items in record.items():
            page = out[out_index[pno]]
            out_words = page.get_text("words")
            for rect, method, original in items:
                probe = fitz.Rect(rect.x0 + rect.width * 0.2, rect.y0 + rect.height * 0.25,
                                  rect.x1 - rect.width * 0.2, rect.y1 - rect.height * 0.25)
                got = page.get_textbox(probe).strip() if not probe.is_empty else ""
                if method == BLACKOUT and got and _norm(got) and _norm(got) in _norm(original):
                    failures.append({"page": pno + 1, "text": got})
                # Whole words here: the probe trims narrow letters at a word's edges, so `got`
                # for a surviving "Wi" is "W". Punctuation is kept by design, so it can't fail.
                if method == SCRAMBLE and _can_scramble(original) and any(
                        _norm(w[4]) == _norm(original) and fitz.Rect(w[:4]).intersects(probe)
                        for w in out_words):
                    failures.append({"page": pno + 1, "text": original})
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
        checks.append(self._check_hidden_data(out, record, unparsed))

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
                gone = self.concealed.get(pno, ())  # lifted out, so not a repeat anyone can see
                idx = [i for i, w in enumerate(words) if hits(t, _norm(w[4]))
                       and i not in selected.get(pno, set())
                       and not any(fitz.Rect(w[:4]).intersects(r) for r in gone)]
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
        orphans = _orphan_pages(out)
        if orphans:
            problems.append("%d page object(s) still recoverable from the file" % len(orphans))

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
