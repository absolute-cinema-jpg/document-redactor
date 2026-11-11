# Document Sanitization Bureau — Form RD-61

A local, privacy-first tool for removing sensitive information from PDFs. Open a
PDF and mark what has to go: names, addresses, account numbers, signatures,
photos, whole pages. You get a copy where that material is **actually
destroyed**, not just covered with a black box. Everything you didn't mark,
including the layout, fonts and tables, is left as it was, so the document still
looks and reads like the original.

Everything happens on your own computer. Nothing is uploaded, and there are no
accounts and no network connections.

## The look

The interface is styled after a 1960s CIA records office: manila folders,
typewriter type, rubber-stamp buttons, and a classification banner that turns
from red **TOP SECRET** to green **CLEARED FOR RELEASE** once a document has
passed inspection. Blackouts are rough, hand-inked marker bars, modelled on
declassified Cold War memos, and they appear that way in the finished PDF too.
The app uses only fonts already on your computer, so it never has to download
anything.

## Running it

**As a desktop app (easiest, and what to give other people).** The app opens
in its own window: no terminal, no browser tab, no ports. "Download" shows a
normal Save dialog, and closing the window shuts the app down and erases the
document from memory. Python and every library are bundled inside, so the
people you share it with don't install anything.

There are three builds, each zipped together with a `READ ME FIRST.txt` that
explains how to open the app the first time:

| Zip | For | Built with |
|---|---|---|
| `dist/Sanitization-Bureau-mac-arm64.zip` | Apple Silicon Macs (M1 and later) | `packaging/build_mac.sh arm64` |
| `dist/Sanitization-Bureau-mac-x86_64.zip` | Intel Macs (also runs on Apple Silicon) | `packaging/build_mac.sh x86_64`, which uses Rosetta on an Apple Silicon Mac |
| `dist/Sanitization-Bureau-windows-x64.zip` | Windows 10 and 11 | `packaging\build_windows.ps1`, on a Windows PC |

Every build self-tests the finished app (it opens the real window, sanitizes a
sample and checks the result) before zipping it, and refuses to package a
broken app.

A Windows app can only be built on Windows. If you don't have a Windows PC,
`.github/workflows/build.yml` builds all three on GitHub's machines. Push the
project to a GitHub repository, open **Actions > Build apps > Run workflow**,
and download the zips from the finished run's **Artifacts**.

- **First launch warning.** The apps aren't signed with a paid developer
  certificate. The first time someone opens one, macOS says it "could not
  verify" it, and Windows shows "Windows protected your PC". The readme in each
  zip walks people through allowing it, which only has to happen once.
  Removing the warning needs an Apple Developer account ($99/year) for the
  Macs and a code-signing certificate for Windows.

**From the terminal (for development).**

```bash
./run.sh
```

This needs Python 3.11 or later, which macOS doesn't include: install it from
[python.org](https://www.python.org/downloads/) or with `uv python install 3.13`.
The first run creates a private Python environment (`.venv/`) and installs
PyMuPDF, Flask and fontTools. After that it opens http://127.0.0.1:1961 in your
browser. Press Ctrl+C in the terminal to shut it down. If that port is busy, it
uses the next free one.

To run the windowed version from source:

```bash
.venv/bin/python -m redactor.desktop
```

This needs `pip install -r packaging/requirements-build.txt` first.

## Workflow

1. **Intake.** Drop a PDF on the in-tray. Password-protected files ask for the password.
   The "Hidden cargo" card lists material the file carries that you can't see:
   metadata, comments, bookmarks, attachments, invisible OCR text. All of it is
   destroyed automatically.
2. **Review.** Mark what to redact.
   - **Text tool (T).** Drag across words, or drag on empty space to box-select.
     Click a marked word to release it.
   - **Area tool (A).** Drag a region for signatures, photos, logos or stamps.
     Click an outlined image to strike all of it. Click a struck region to release it.
   - **Method.** **Blackout (B)** draws a rough ink bar. **Scramble (S)** replaces
     every letter and digit with a random one of the same kind and keeps the
     punctuation, so `PX-2291-4471-0093` becomes something like `TK-1965-0744-8388`
     and `j.smithers@example.com` becomes `l.owsfoszj@eizowoe.oen`. Click the swatch
     next to a mark in the dossier to switch its method.
   - **Search & mark (/).** Search for a word or phrase, or use the presets (emails,
     phones, dates, IDs and account numbers, money, URLs). The search box also
     takes regular expressions. Enter jumps to the next match, and "Mark all"
     strikes every match.
   - **Withdraw pages.** Use the button above each page, or type pages and
     ranges such as `3, 7-12` into the sidebar box, to drop whole pages from the
     sanitized copy. Withdrawn pages stay visible, stamped WITHDRAWN, so you can
     reinstate them. At least one page has to remain.
   - Ctrl/Cmd+Z undoes, including withdrawals.
3. **Sanitize.** The debrief screen shows:
   - the rendered output,
   - an **inspection report** that re-opens the output and verifies that each
     marked word is gone from the text layer, metadata is empty, and nothing
     hidden remains (scripts, links, attachments, unapplied annotations),
   - **possible leaks**: terms you struck in one place that still appear
     elsewhere, including inside emails and URLs and words printed in a scanned
     image. One click marks them all.
   - **text left in the file**: the full text layer of the output, i.e.
     everything anyone could copy, search or extract from it.
4. **Download** the result as `SANITIZED-XXXXXX.pdf`. The name is neutral because
   the original filename can itself be confidential. **Burn & exit** wipes the
   document from memory. Closing the tab does the same.

## What "destroyed" means here

| Thing | What happens |
|---|---|
| Withdrawn pages | Removed from the page tree, then garbage-collected along with any fonts and images only they used. Everything that could keep a page object alive (accessibility tags, form fields, named destinations) goes too. The inspection report verifies that their words, images and page objects are absent. |
| Marked text | Removed from the page content stream (MuPDF redaction). The bar or scramble is drawn afterwards, on top of nothing. |
| Images under a mark | The pixels inside the mark are overwritten in the image itself, and in its transparency mask. |
| Vector art in an *area* | Removed if fully covered, e.g. a signature drawn as paths. Word marks leave line art alone so table rules and underlines survive. |
| Invisible text (OCR layers) | Stripped by default. There is an option to retain it. |
| Concealed text | Text under a drawn box or image, text that blends into its background, and text under 1.5pt is removed. The page is rendered with and without its text, and a run that changes no pixels counts as concealed. |
| Hidden layers | Content on layers that are off is removed. Layers that are on become ordinary page content. |
| Anything outside the page | Text, images and art off the page or outside a crop are removed. |
| Metadata (Info dictionary, XMP, JPEG EXIF/GPS) | Wiped. Only the keys rendering needs are kept on the catalog, pages and images, so private data under unknown keys goes too. |
| Comments, notes, stamps, other annotations | Deleted. |
| Form fields | Removed, with their values, names, tooltips and defaults. The output isn't fillable. |
| Accessibility tags | Removed, because they hold alt text and point back at pages. **The output is an untagged PDF**, which is worse for screen readers. |
| Bookmarks, links, attachments, JavaScript, thumbnails | Removed. |
| Fonts | Re-subset, best effort, so glyphs used only by redacted text are dropped. |
| The file itself | Rebuilt from scratch (`garbage=4`) with no incremental-save history, and decrypted. |

The app itself:

- binds to `127.0.0.1` only and refuses requests for any other host name, which
  blocks DNS-rebinding attacks from web pages,
- keeps documents in RAM only (uploads are never spooled to a temp file),
- sends `Cache-Control: no-store` so the browser doesn't cache rendered pages to disk,
- loads no external fonts, scripts or CDNs, so it works fully offline.

## Know the limits

- **Shape is preserved on purpose.** Both methods reveal roughly how long the
  hidden text was. Scramble also keeps the character classes (letter, digit,
  punctuation) and capitalisation. That is the point for format learning, but
  it is a signal. The random characters come from OS entropy and can't be
  reversed. Scramble picks the candidate whose width best fits the original
  slot, and that width is already visible on the page. It never keeps the
  original word. A word with no letters or digits, such as `&` or `---`, has
  nothing to scramble and stays as it is, so use Blackout for those.
- **Concealed text that overlaps visible text** stays, because lifting it could
  delete something you can see. Its box changes pixels, so it doesn't count as
  concealed. If an unexpected word shows up in the text layer in the debrief,
  strike that line with **Area**.
- **Pixels hidden by a clipping path** stay. An image cropped by a clip inside the
  page keeps its full data. Off-page parts are wiped, but use **Area** on anything
  sensitive that a clip is only hiding. JPEG 2000 images keep any embedded metadata.
- **Scanned pages.** Selectable text on a scan is an OCR layer that may not sit
  exactly on the printed glyphs. Strikes on scanned pages are padded slightly,
  but always check the released preview and use **Area** for anything that
  peeks out. The app flags scanned pages at intake.
- **Stream order.** Scrambled replacement text is written at the end of each
  page's content stream. Layout-aware extractors (pdfplumber, PyMuPDF with
  `sort=True`, most ML pipelines) see it in the right place. Naive stream-order
  extraction sees it at the end of the page.
- Scrambled text uses the document's own embedded font when it has the glyphs.
  Otherwise it falls back to the closest standard font (Helvetica, Times or
  Courier).

## Tests

```bash
.venv/bin/python tests/test_engine.py [output_dir]
.venv/bin/python tests/test_hidden_data.py
```

`test_hidden_data.py` plants data everywhere a PDF can hide it (tags, hidden layers,
form fields, off-page and covered text, private keys, transparency masks, JPEG EXIF),
then searches every object and decoded stream of the output for it. It also proves
the inspection checks fail when that data is really there.

This builds a fake insurance claim full of traps: metadata, XMP, a sticky
note holding an SSN, a link, bookmarks, an attachment, subset-embedded fonts,
an image and a rotated page. It redacts the claim, then checks the output
three ways:

1. MuPDF text extraction,
2. a grep of the raw file,
3. a decode of every string in every decompressed stream.

It also checks a scanned page with an invisible OCR layer. The stream scanner
is checked against the original document to prove it detects the secrets
before redaction.

## Layout

```
run.sh                  terminal launcher (creates .venv on first run)
redactor/engine.py      redaction, sanitising and verification (PyMuPDF)
redactor/server.py      local-only Flask server
redactor/desktop.py     desktop window (pywebview) + native Save dialog
packaging/              app build scripts (Mac, Windows), icon, recipient readmes
.github/workflows/      builds all three apps on GitHub
redactor/static/        the interface (no build step, no external assets)
tests/                  sample generator + end-to-end test
```
