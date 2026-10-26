"use strict";
/* Document Sanitization Bureau -- client.
   Talks only to the local server it was served from. */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
const SVGNS = "http://www.w3.org/2000/svg";

const S = {
  id: null,
  pages: [],          // [{w, h, words:[{r,t,b,l}], images:[[x0,y0,x1,y1]]}]
  summary: null,
  marks: [],          // [{id, page, type:"words"|"area", words?, rect?, method}]
  removed: new Set(), // page indices withdrawn from the output
  nextId: 1,
  undo: [],
  tool: "text",
  method: "blackout",
  els: [],            // per page {sheet, svg, gMarks, gCand, gTemp}
  index: [],          // per page search index {str, starts}
  candidates: [],     // [{page, words}]
  candCursor: -1,
  drag: null,
  report: null,
  pendingFile: null,
};

const PRESETS = {
  email: String.raw`[\w.+-]+@[\w-]+(\.[\w-]+)+`,
  phone: String.raw`(\+\d{1,3}[\s.-]?)?(\(\d{2,5}\)[\s.-]?|\d{2,5}[\s.-])\d{3,4}[\s.-]?\d{3,4}`,
  date: String.raw`\b(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|\d{4}-\d{2}-\d{2}|\d{1,2}(st|nd|rd|th)? (jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,? \d{2,4}|(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? \d{1,2}(st|nd|rd|th)?,? \d{4})\b`,
  ids: String.raw`\b(?=[A-Z0-9-]*\d{3})[A-Z0-9][A-Z0-9-]{4,}\b|\b\d{3,}(?: \d{2,})+\b`,
  money: String.raw`[$£€¥]\s?\d[\d,]*(\.\d+)?|\b\d[\d,]*(\.\d{2})? ?(usd|gbp|eur|dollars|pounds)\b`,
  url: String.raw`\b(https?://|www\.)[^\s]+`,
};

const HINTS = {
  text: "Drag across words to mark them. Drag on empty space to box-select. Click a marked word to release it.",
  area: "Drag to strike a region — signatures, photos, logos. Click an outlined image to strike it whole. Click a struck region to release it.",
};

const norm = (t) => t.replace(/­/g, "-");

/* ------------------------------------------------------------------ */
/* screens & banners                                                   */
/* ------------------------------------------------------------------ */

function setScreen(name) { document.body.dataset.screen = name; }

function setClass(kind, text) {
  document.body.dataset.class = kind;
  $("#banner").textContent = text;
  $("#banner-bottom").textContent = text;
}

function busy(on, title) {
  $("#busy").hidden = !on;
  if (title) $("#busy-title").textContent = title;
}

/* ------------------------------------------------------------------ */
/* intake                                                              */
/* ------------------------------------------------------------------ */

async function openFile(file, password) {
  $("#intake-msg").textContent = "";
  if (!file) return;
  busy(true, "Receiving");
  try {
    const buf = await file.arrayBuffer();
    const headers = { "Content-Type": "application/pdf" };
    if (password) headers["X-Pdf-Password"] = password;
    const res = await fetch("/api/open", { method: "POST", body: buf, headers });
    const data = await res.json();
    if (res.status === 401) {
      S.pendingFile = file;
      $("#pw").value = "";
      $("#pw-modal").hidden = false;
      $("#pw").focus();
      return;
    }
    if (!res.ok) throw new Error(data.error || "Could not open document.");
    S.pendingFile = null;
    initReview(data);
  } catch (err) {
    $("#intake-msg").textContent = err.message;
  } finally {
    busy(false);
    $("#file").value = "";
  }
}

function initReview(info) {
  S.id = info.id;
  S.pages = info.pages;
  S.summary = info.summary;
  S.marks = [];
  S.removed = new Set();
  S.undo = [];
  S.nextId = 1;
  S.candidates = [];
  S.report = null;
  buildIndex();
  buildPages();
  renderCargo();
  renderDossier();
  $("#search").value = "";
  $("#search-count").innerHTML = "&nbsp;";
  $("#mark-all").disabled = true;
  setScreen("review");
  setClass("secret", "TOP SECRET // CONTAINS UNSANITIZED MATERIAL");
  $("#pages").scrollTop = 0;
}

function renderCargo() {
  const s = S.summary;
  const items = [];
  if (s.metadata_fields.length) items.push(`document properties: ${s.metadata_fields.join(", ")}`);
  if (s.has_xmp) items.push("XMP metadata packet");
  if (s.annotations) items.push(`${s.annotations} comment(s) / annotation(s)`);
  if (s.bookmarks) items.push(`${s.bookmarks} bookmark(s)`);
  if (s.embedded_files) items.push(`${s.embedded_files} attached file(s)`);
  if (s.form_fields) items.push(`${s.form_fields} form field value(s) — reset`);
  if (s.invisible_spans) items.push(`${s.invisible_spans} run(s) of invisible text (unless retained below)`);
  const el = $("#cargo");
  el.innerHTML = "";
  if (!s.word_count) {
    const p = document.createElement("p");
    p.innerHTML = "<b>No text layer found.</b> This looks like a scanned image. Use the <b>AREA</b> instrument to strike regions.";
    el.appendChild(p);
  } else if (s.scanned_pages.length) {
    const p = document.createElement("p");
    p.className = "caution";
    p.innerHTML = `<b>Scanned page(s): ${s.scanned_pages.join(", ")}.</b> The selectable text there is an OCR layer and may not sit exactly on the printed words. Check the released preview closely and use <b>AREA</b> for anything that peeks out.`;
    el.appendChild(p);
  }
  if (items.length) {
    const h = document.createElement("h2");
    h.textContent = "Hidden cargo";
    const p = document.createElement("div");
    p.textContent = "Found in this file and destroyed automatically on sanitize:";
    const ul = document.createElement("ul");
    for (const t of items) { const li = document.createElement("li"); li.textContent = t; ul.appendChild(li); }
    el.append(h, p, ul);
  }
}

/* ------------------------------------------------------------------ */
/* page rendering                                                      */
/* ------------------------------------------------------------------ */

function svgEl(tag, attrs) {
  const el = document.createElementNS(SVGNS, tag);
  for (const k in attrs) el.setAttribute(k, attrs[k]);
  return el;
}

function rectEl(r, cls) {
  return svgEl("rect", { x: r[0], y: r[1], width: Math.max(0.5, r[2] - r[0]), height: Math.max(0.5, r[3] - r[1]), class: cls });
}

function ensureDefs() {
  if ($("#defs-svg")) return;
  const svg = svgEl("svg", { id: "defs-svg", width: 0, height: 0, style: "position:absolute" });
  const pat = svgEl("pattern", { id: "hatch", width: 3, height: 3, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)" });
  pat.appendChild(svgEl("rect", { width: 3, height: 3, fill: "rgba(168,35,27,0.12)" }));
  pat.appendChild(svgEl("rect", { width: 1.1, height: 3, fill: "rgba(168,35,27,0.75)" }));
  const defs = svgEl("defs", {});
  defs.appendChild(pat);
  svg.appendChild(defs);
  document.body.prepend(svg);
}

function makeSheet(n, total, imgSrc, w, h, note = "") {
  const sheet = document.createElement("div");
  sheet.className = "sheet";
  const label = document.createElement("div");
  label.className = "sheet-label";
  label.innerHTML = `<span>PAGE ${n + 1} OF ${total}${note}</span><span class="right">${Math.round(w)} × ${Math.round(h)} PT</span>`;
  const paper = document.createElement("div");
  paper.className = "paper";
  const img = document.createElement("img");
  img.loading = n < 2 ? "eager" : "lazy";
  img.decoding = "async";
  img.width = Math.round(w * 2);
  img.height = Math.round(h * 2);
  img.alt = `Page ${n + 1}`;
  img.src = imgSrc;
  img.draggable = false;
  paper.appendChild(img);
  sheet.append(label, paper);
  return { sheet, paper, label };
}

function buildPages() {
  ensureDefs();
  const host = $("#pages");
  host.innerHTML = "";
  S.els = [];
  S.pages.forEach((p, n) => {
    const { sheet, paper, label } = makeSheet(n, S.pages.length, `/api/doc/${S.id}/page/${n}.png?zoom=2`, p.w, p.h);
    const btn = document.createElement("button");
    btn.className = "page-btn";
    btn.addEventListener("click", () => togglePage(n));
    $(".right", label).prepend(btn);
    const svg = svgEl("svg", { viewBox: `0 0 ${p.w} ${p.h}`, preserveAspectRatio: "none" });
    const gWords = svgEl("g", {});
    p.words.forEach((w, i) => { const r = rectEl(w.r, "w"); r.dataset.i = i; gWords.appendChild(r); });
    const gImgs = svgEl("g", {});
    p.images.forEach((r) => gImgs.appendChild(rectEl(r, "img-hot")));
    const gCand = svgEl("g", { "pointer-events": "none" });
    const gMarks = svgEl("g", { "pointer-events": "none" });
    const gTemp = svgEl("g", { "pointer-events": "none" });
    svg.append(gImgs, gWords, gCand, gMarks, gTemp);
    paper.appendChild(svg);
    host.appendChild(sheet);
    S.els[n] = { sheet, svg, gMarks, gCand, gTemp, btn };
    attachPointer(svg, n);
    applyWithdrawn(n);
  });
}

function applyWithdrawn(n) {
  const { sheet, btn } = S.els[n];
  const out = S.removed.has(n);
  sheet.classList.toggle("withdrawn", out);
  btn.textContent = out ? "Reinstate page" : "Withdraw page";
  btn.title = out ? "Keep this page in the sanitized copy" : "Remove this whole page from the sanitized copy";
}

function renderMarks(n) {
  const { gMarks } = S.els[n];
  gMarks.textContent = "";
  const frag = document.createDocumentFragment();
  const words = S.pages[n].words;
  for (const m of S.marks) {
    if (m.page !== n) continue;
    if (m.type === "area") {
      const r = rectEl(m.rect, `m-area m-${m.method}`);
      r.dataset.mark = m.id;
      frag.appendChild(r);
    } else {
      for (const i of m.words) {
        const r = rectEl(words[i].r, `m-${m.method}`);
        r.dataset.mark = m.id;
        frag.appendChild(r);
      }
    }
  }
  gMarks.appendChild(frag);
}

function renderAllMarks() {
  S.pages.forEach((_, n) => { renderMarks(n); applyWithdrawn(n); });
  renderDossier();
}

/* ------------------------------------------------------------------ */
/* marks model                                                         */
/* ------------------------------------------------------------------ */

function pushUndo() {
  S.undo.push(JSON.stringify({ marks: S.marks, removed: [...S.removed] }));
  if (S.undo.length > 200) S.undo.shift();
}

function undo() {
  if (!S.undo.length) return;
  const snap = JSON.parse(S.undo.pop());
  S.marks = snap.marks;
  S.removed = new Set(snap.removed);
  renderAllMarks();
}

function detachWords(page, idxs) {
  const set = new Set(idxs);
  for (const m of S.marks) {
    if (m.page === page && m.type === "words") m.words = m.words.filter((i) => !set.has(i));
  }
  S.marks = S.marks.filter((m) => m.type !== "words" || m.words.length);
}

function addWordMark(page, idxs, method = S.method, record = true) {
  if (!idxs.length) return;
  if (record) pushUndo();
  detachWords(page, idxs);
  S.marks.push({ id: S.nextId++, page, type: "words", words: [...new Set(idxs)].sort((a, b) => a - b), method });
  if (record) { renderMarks(page); renderDossier(); }
}

function releaseWords(page, idxs) {
  pushUndo();
  detachWords(page, idxs);
  renderMarks(page);
  renderDossier();
}

function addArea(page, rect) {
  pushUndo();
  S.marks.push({ id: S.nextId++, page, type: "area", rect: rect.map((v) => Math.round(v * 100) / 100), method: S.method });
  renderMarks(page);
  renderDossier();
}

function removeMark(id) {
  const m = S.marks.find((x) => x.id === id);
  if (!m) return;
  pushUndo();
  S.marks = S.marks.filter((x) => x.id !== id);
  renderMarks(m.page);
  renderDossier();
}

function toggleMethod(id) {
  const m = S.marks.find((x) => x.id === id);
  if (!m) return;
  pushUndo();
  m.method = m.method === "blackout" ? "scramble" : "blackout";
  renderMarks(m.page);
  renderDossier();
}

function withdrawPages(pages, on) {
  const next = new Set(S.removed);
  pages.forEach((n) => (on ? next.add(n) : next.delete(n)));
  if (next.size >= S.pages.length) {
    alert("At least one page has to remain in the sanitized copy.");
    return false;
  }
  pushUndo();
  S.removed = next;
  pages.forEach(applyWithdrawn);
  renderDossier();
  runSearch();
  return true;
}

function togglePage(n) { withdrawPages([n], !S.removed.has(n)); }

function parsePageList(text) {
  const out = new Set();
  for (const part of text.split(/[\s,;]+/).filter(Boolean)) {
    const m = part.match(/^(\d+)(?:-(\d+))?$/);
    if (!m) throw new Error(`"${part}" is not a page or range`);
    const a = +m[1], b = m[2] ? +m[2] : a;
    if (a < 1 || b > S.pages.length || a > b) throw new Error(`pages run from 1 to ${S.pages.length}`);
    for (let k = a; k <= b; k++) out.add(k - 1);
  }
  return [...out];
}

function withdrawFromInput() {
  const msg = $("#pages-msg");
  try {
    const pages = parsePageList($("#page-list").value);
    if (!pages.length) return;
    if (withdrawPages(pages, true)) {
      $("#page-list").value = "";
      msg.textContent = "";
    }
  } catch (err) {
    msg.textContent = err.message;
  }
}

function markExcerpt(m) {
  if (m.type === "area") {
    const [x0, y0, x1, y1] = m.rect;
    return `[region ${Math.round(x1 - x0)}×${Math.round(y1 - y0)} pt]`;
  }
  return m.words.map((i) => norm(S.pages[m.page].words[i].t)).join(" ");
}

function markSortKey(m) {
  const y = m.type === "area" ? m.rect[1] : S.pages[m.page].words[m.words[0]].r[1];
  return m.page * 1e6 + y;
}

function renderDossier() {
  const ul = $("#marks");
  ul.textContent = "";
  for (const n of [...S.removed].sort((a, b) => a - b)) {
    const li = document.createElement("li");
    li.className = "withdrawn-row";
    li.innerHTML = `<span class="pg">p.${n + 1}</span><span class="sw wd-sw"></span><span class="ex">[whole page withdrawn]</span>`;
    const x = document.createElement("button");
    x.className = "x";
    x.textContent = "×";
    x.title = "Reinstate this page";
    x.addEventListener("click", (e) => { e.stopPropagation(); togglePage(n); });
    li.appendChild(x);
    li.addEventListener("click", () => S.els[n].sheet.scrollIntoView({ behavior: "smooth", block: "start" }));
    ul.appendChild(li);
  }
  const sorted = [...S.marks].sort((a, b) => markSortKey(a) - markSortKey(b));
  for (const m of sorted) {
    const li = document.createElement("li");
    const pg = document.createElement("span");
    pg.className = "pg";
    pg.textContent = `p.${m.page + 1}`;
    const sw = document.createElement("span");
    sw.className = `sw ${m.method === "blackout" ? "ink-sw" : "scr-sw"}`;
    sw.title = `${m.method} — click to switch`;
    sw.addEventListener("click", (e) => { e.stopPropagation(); toggleMethod(m.id); });
    const ex = document.createElement("span");
    ex.className = "ex";
    ex.textContent = markExcerpt(m);
    ex.title = ex.textContent;
    const x = document.createElement("button");
    x.className = "x";
    x.textContent = "×";
    x.title = "Release this mark";
    x.addEventListener("click", (e) => { e.stopPropagation(); removeMark(m.id); });
    li.append(pg, sw, ex, x);
    if (S.removed.has(m.page)) {
      li.classList.add("moot");
      li.title = "On a withdrawn page — the whole page is removed anyway";
    }
    li.addEventListener("click", () => revealMark(m));
    ul.appendChild(li);
  }
  $("#mark-count").textContent = S.marks.length + S.removed.size;
  $("#marks-empty").hidden = S.marks.length + S.removed.size > 0;
  const k = S.removed.size, total = S.pages.length;
  $("#pages-status").textContent = k
    ? `${k} of ${total} withdrawn · ${total - k} will remain`
    : `All ${total} page${total > 1 ? "s" : ""} kept`;
  $("#reinstate-all").hidden = !k;
  $("#undo").disabled = !S.undo.length;
}

function revealMark(m) {
  const { sheet, gMarks } = S.els[m.page];
  const rects = $$(`[data-mark="${m.id}"]`, gMarks);
  (rects[0] || sheet).scrollIntoView({ behavior: "smooth", block: "center" });
  rects.forEach((r) => { r.classList.remove("flash"); void r.getBBox(); r.classList.add("flash"); });
}

/* ------------------------------------------------------------------ */
/* pointer interaction                                                 */
/* ------------------------------------------------------------------ */

function toPage(svg, e) {
  const pt = svg.createSVGPoint();
  pt.x = e.clientX;
  pt.y = e.clientY;
  return pt.matrixTransform(svg.getScreenCTM().inverse());
}

const inside = (r, p, pad = 0) => p.x >= r[0] - pad && p.x <= r[2] + pad && p.y >= r[1] - pad && p.y <= r[3] + pad;
const normRect = (a, b) => [Math.min(a.x, b.x), Math.min(a.y, b.y), Math.max(a.x, b.x), Math.max(a.y, b.y)];
const overlaps = (a, b) => a[0] < b[2] && a[2] > b[0] && a[1] < b[3] && a[3] > b[1];

function hitWord(n, p) {
  const words = S.pages[n].words;
  for (let i = 0; i < words.length; i++) if (inside(words[i].r, p, 0.5)) return i;
  return -1;
}

function wordMarked(n, i) {
  return S.marks.some((m) => m.page === n && m.type === "words" && m.words.includes(i));
}

function wordsInRect(n, r) {
  const out = [];
  S.pages[n].words.forEach((w, i) => { if (overlaps(w.r, r)) out.push(i); });
  return out;
}

function drawTemp(n) {
  const d = S.drag;
  const { gTemp } = S.els[n];
  gTemp.textContent = "";
  if (!d) return;
  const words = S.pages[n].words;
  let idxs = [];
  if (d.kind === "range") {
    const [a, b] = [Math.min(d.anchor, d.cur), Math.max(d.anchor, d.cur)];
    for (let i = a; i <= b; i++) idxs.push(i);
  } else {
    const r = normRect(d.p0, d.p1);
    gTemp.appendChild(rectEl(r, "marquee"));
    if (d.kind === "marquee") idxs = wordsInRect(n, r);
  }
  for (const i of idxs) gTemp.appendChild(rectEl(words[i].r, "sel"));
}

function attachPointer(svg, n) {
  svg.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    const p = toPage(svg, e);
    svg.setPointerCapture(e.pointerId);
    if (S.tool === "text") {
      const wi = hitWord(n, p);
      S.drag = wi >= 0 ? { kind: "range", page: n, anchor: wi, cur: wi, moved: false }
                       : { kind: "marquee", page: n, p0: p, p1: p };
    } else {
      S.drag = { kind: "area", page: n, p0: p, p1: p };
    }
    drawTemp(n);
  });

  svg.addEventListener("pointermove", (e) => {
    const d = S.drag;
    if (!d || d.page !== n) return;
    const p = toPage(svg, e);
    if (d.kind === "range") {
      const wi = hitWord(n, p);
      if (wi >= 0 && wi !== d.cur) { d.cur = wi; d.moved = true; drawTemp(n); }
    } else {
      d.p1 = p;
      drawTemp(n);
    }
  });

  const finish = (e) => {
    const d = S.drag;
    if (!d || d.page !== n) return;
    S.drag = null;
    S.els[n].gTemp.textContent = "";
    if (e.type === "pointercancel") return;
    if (d.kind === "range") {
      if (!d.moved) {
        if (wordMarked(n, d.anchor)) releaseWords(n, [d.anchor]);
        else addWordMark(n, [d.anchor]);
      } else {
        const [a, b] = [Math.min(d.anchor, d.cur), Math.max(d.anchor, d.cur)];
        addWordMark(n, Array.from({ length: b - a + 1 }, (_, k) => a + k));
      }
      return;
    }
    const r = normRect(d.p0, d.p1);
    const tiny = r[2] - r[0] < 4 && r[3] - r[1] < 4;
    if (d.kind === "marquee") {
      if (!tiny) addWordMark(n, wordsInRect(n, r));
      return;
    }
    if (tiny) {
      const hit = [...S.marks].reverse().find((m) => m.page === n && m.type === "area" && inside(m.rect, d.p0));
      if (hit) { removeMark(hit.id); return; }
      const img = S.pages[n].images.find((ir) => inside(ir, d.p0));
      if (img) addArea(n, img);
      return;
    }
    addArea(n, r);
  };
  svg.addEventListener("pointerup", finish);
  svg.addEventListener("pointercancel", finish);
}

/* ------------------------------------------------------------------ */
/* search                                                              */
/* ------------------------------------------------------------------ */

function buildIndex() {
  S.index = S.pages.map((p) => {
    const starts = [];
    let str = "";
    p.words.forEach((w, i) => {
      if (i) str += " ";
      starts.push(str.length);
      str += norm(w.t);
    });
    return { str, starts };
  });
}

function wordsForSpan(n, a, b) {
  const { starts } = S.index[n];
  const words = S.pages[n].words;
  const out = [];
  for (let i = 0; i < starts.length; i++) {
    const s = starts[i], e = s + norm(words[i].t).length;
    if (s < b && e > a) out.push(i);
    if (s >= b) break;
  }
  return out;
}

function runSearch() {
  const q = $("#search").value;
  S.candidates = [];
  S.candCursor = -1;
  S.els.forEach((el) => { el.gCand.textContent = ""; });
  $$("#presets button").forEach((b) => b.classList.toggle("on", $("#regex").checked && PRESETS[b.dataset.preset] === q));
  if (!q.trim()) {
    $("#search-count").innerHTML = "&nbsp;";
    $("#mark-all").disabled = true;
    $("#mark-all").textContent = "Mark all";
    return;
  }
  let re;
  try {
    const src = $("#regex").checked ? q : q.trim().replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/\s+/g, "\\s+");
    re = new RegExp(src, "gi");
  } catch (err) {
    $("#search-count").textContent = "pattern not understood";
    $("#mark-all").disabled = true;
    return;
  }
  const pagesHit = new Set();
  S.index.forEach(({ str }, n) => {
    if (S.removed.has(n)) return;
    re.lastIndex = 0;
    let m;
    while ((m = re.exec(str))) {
      if (!m[0].length) { re.lastIndex++; continue; }
      const idxs = wordsForSpan(n, m.index, m.index + m[0].length);
      if (idxs.length) { S.candidates.push({ page: n, words: idxs }); pagesHit.add(n); }
    }
  });
  for (const c of S.candidates) {
    for (const i of c.words) S.els[c.page].gCand.appendChild(rectEl(S.pages[c.page].words[i].r, "cand"));
  }
  const k = S.candidates.length;
  $("#search-count").textContent = k ? `${k} match${k > 1 ? "es" : ""} on ${pagesHit.size} page${pagesHit.size > 1 ? "s" : ""}` : "no matches";
  $("#mark-all").disabled = !k;
  $("#mark-all").textContent = k ? `Mark all ${k}` : "Mark all";
}

function nextCandidate() {
  if (!S.candidates.length) return;
  S.candCursor = (S.candCursor + 1) % S.candidates.length;
  const c = S.candidates[S.candCursor];
  const rects = $$(".cand", S.els[c.page].gCand);
  const w = S.pages[c.page].words[c.words[0]].r;
  const target = rects.find((r) => +r.getAttribute("x") === w[0] && +r.getAttribute("y") === w[1]) || S.els[c.page].sheet;
  target.scrollIntoView({ behavior: "smooth", block: "center" });
}

function markAllCandidates() {
  if (!S.candidates.length) return;
  pushUndo();
  const touched = new Set();
  for (const c of S.candidates) { addWordMark(c.page, c.words, S.method, false); touched.add(c.page); }
  touched.forEach(renderMarks);
  renderDossier();
  $("#search").value = "";
  runSearch();
}

/* ------------------------------------------------------------------ */
/* processing & debrief                                                */
/* ------------------------------------------------------------------ */

async function processDoc() {
  if (!S.marks.length && !S.removed.size &&
      !confirm("No marks placed. Sanitize anyway? Metadata, comments, links and attachments will still be destroyed.")) return;
  busy(true, "Sanitizing");
  try {
    const res = await fetch(`/api/doc/${S.id}/process`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        marks: S.marks.filter((m) => !S.removed.has(m.page)),
        remove_pages: [...S.removed],
        keep_hidden_text: $("#keep-hidden").checked,
      }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || "The bureau could not process this document.");
    }
    S.report = await res.json();
    showDebrief();
  } catch (err) {
    alert(err.message);
  } finally {
    busy(false);
  }
}

function showDebrief() {
  const r = S.report;
  const hardFail = r.checks.some((c) => !c.ok && !c.warn_only);
  const ul = $("#checks");
  ul.textContent = "";
  for (const c of r.checks) {
    const li = document.createElement("li");
    li.className = c.ok ? "ok" : c.warn_only ? "warn" : "bad";
    li.innerHTML = `<span class="mk">${c.ok ? "✓" : c.warn_only ? "!" : "✗"}</span><span><span class="l"></span><span class="d"></span></span>`;
    $(".l", li).textContent = c.label;
    $(".d", li).textContent = c.detail;
    ul.appendChild(li);
  }
  const s = r.stats;
  $("#stats").textContent =
    `${s.blackout_words} words blacked out · ${s.scrambled_words} scrambled · ${s.areas} region(s) · ` +
    `${s.pages_touched} page(s) altered · ` +
    (s.pages_removed ? `${s.pages_removed} page(s) withdrawn · ` : "") +
    `${(r.size_bytes / 1024).toFixed(0)} KB`;

  const leaks = $("#leaks");
  leaks.textContent = "";
  $("#leaks-card").hidden = !r.leaks.length;
  for (const l of r.leaks) {
    const li = document.createElement("li");
    const b = document.createElement("b");
    b.textContent = norm(l.term);
    li.append(b, ` — page ${l.pages.join(", ")}`);
    leaks.appendChild(li);
  }

  const host = $("#out-pages");
  host.innerHTML = "";
  const stamp = Date.now();
  for (let n = 0; n < r.page_count; n++) {
    const orig = r.kept_pages[n];
    const p = S.pages[orig];
    const note = orig !== n ? ` · ORIGINALLY PAGE ${orig + 1}` : "";
    const { sheet, paper } = makeSheet(n, r.page_count, `/api/doc/${S.id}/page/${n}.png?out=1&zoom=2&v=${stamp}`, p.w, p.h, note);
    const st = document.createElement("div");
    st.className = "released";
    st.innerHTML = hardFail ? "WITHHELD<small>INSPECTION FAILED</small>" : "RELEASED<small>SANITIZED COPY</small>";
    if (hardFail) { st.style.color = "var(--red)"; st.style.borderColor = "var(--red)"; }
    paper.appendChild(st);
    host.appendChild(sheet);
  }
  $("#download").href = `/api/doc/${S.id}/download`;
  $("#save-msg").textContent = "";
  setScreen("debrief");
  host.scrollTop = 0;
  if (hardFail) setClass("secret", "INSPECTION FAILED // DO NOT RELEASE");
  else setClass("clear", "SANITIZED // CLEARED FOR RELEASE");
}

// In the desktop app there is no browser download bar: ask the app to show
// a native Save dialog instead. In a browser the link downloads as usual.
async function saveOutput(e) {
  const api = window.pywebview && window.pywebview.api;
  if (!api || !api.save_output) return;
  e.preventDefault();
  const msg = $("#save-msg");
  msg.textContent = "";
  try {
    const r = await api.save_output(S.id);
    if (r.saved) msg.textContent = `Filed as ${r.saved}`;
    else if (r.error) msg.textContent = r.error;
  } catch (err) {
    msg.textContent = "Could not save the file.";
  }
}

function markLeaks() {
  if (!S.report) return;
  pushUndo();
  for (const l of S.report.leaks) {
    for (const o of l.occurrences) addWordMark(o.page, o.words, S.method, false);
  }
  backToReview();
  renderAllMarks();
}

function backToReview() {
  setScreen("review");
  setClass("secret", "TOP SECRET // CONTAINS UNSANITIZED MATERIAL");
}

function showTextLayer() {
  const host = $("#text-layer");
  host.textContent = "";
  S.report.text_layer.forEach((t, n) => {
    const h = document.createElement("h3");
    const orig = S.report.kept_pages[n];
    h.textContent = `PAGE ${n + 1}` + (orig !== n ? ` (ORIGINALLY PAGE ${orig + 1})` : "");
    const pre = document.createElement("pre");
    pre.textContent = t.trim() || "(no text)";
    host.append(h, pre);
  });
  $("#text-modal").hidden = false;
}

async function burn() {
  if (S.id && !confirm("Burn all papers? The document, the sanitized copy and every mark will be wiped from memory.")) return;
  if (S.id) await fetch(`/api/doc/${S.id}/burn`, { method: "POST" }).catch(() => {});
  S.id = null;
  S.pages = [];
  S.marks = [];
  S.removed = new Set();
  S.undo = [];
  S.report = null;
  $("#pages").textContent = "";
  $("#out-pages").textContent = "";
  setScreen("intake");
  setClass("unclass", "UNCLASSIFIED // NO DOCUMENT ON FILE");
}

/* ------------------------------------------------------------------ */
/* wiring                                                              */
/* ------------------------------------------------------------------ */

function setTool(t) {
  S.tool = t;
  document.body.dataset.tool = t;
  $$("[data-tool]").forEach((b) => b.classList.toggle("on", b.dataset.tool === t));
  $("#tool-hint").textContent = HINTS[t];
}

function setMethod(m) {
  S.method = m;
  $$("[data-method]").forEach((b) => b.classList.toggle("on", b.dataset.method === m));
}

function wire() {
  const dz = $("#dropzone");
  $("#file").addEventListener("change", (e) => openFile(e.target.files[0]));
  ["dragenter", "dragover"].forEach((ev) => document.addEventListener(ev, (e) => {
    e.preventDefault();
    if (document.body.dataset.screen === "intake") dz.classList.add("over");
  }));
  ["dragleave", "drop"].forEach((ev) => document.addEventListener(ev, (e) => {
    e.preventDefault();
    if (ev === "dragleave" && e.relatedTarget) return;
    dz.classList.remove("over");
  }));
  document.addEventListener("drop", (e) => {
    if (document.body.dataset.screen !== "intake") return;
    const f = e.dataTransfer && e.dataTransfer.files[0];
    if (f) openFile(f);
  });

  $("#pw-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const pw = $("#pw").value;
    $("#pw").value = "";
    $("#pw-modal").hidden = true;
    openFile(S.pendingFile, pw);
  });
  $("#pw-cancel").addEventListener("click", () => { $("#pw-modal").hidden = true; S.pendingFile = null; });

  $$("[data-tool]").forEach((b) => b.addEventListener("click", () => setTool(b.dataset.tool)));
  $$("[data-method]").forEach((b) => b.addEventListener("click", () => setMethod(b.dataset.method)));

  $("#search").addEventListener("input", runSearch);
  $("#search").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); nextCandidate(); } });
  $("#regex").addEventListener("change", runSearch);
  $$("#presets button").forEach((b) => b.addEventListener("click", () => {
    $("#search").value = PRESETS[b.dataset.preset];
    $("#regex").checked = true;
    runSearch();
    nextCandidate();
  }));
  $("#mark-all").addEventListener("click", markAllCandidates);
  $("#withdraw-pages").addEventListener("click", withdrawFromInput);
  $("#page-list").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); withdrawFromInput(); } });
  $("#reinstate-all").addEventListener("click", () => withdrawPages([...S.removed], false));

  $("#undo").addEventListener("click", undo);
  $("#clear-marks").addEventListener("click", () => {
    if (!S.marks.length) return;
    pushUndo();
    S.marks = [];
    renderAllMarks();
  });
  $("#process").addEventListener("click", processDoc);
  $$("[data-burn]").forEach((b) => b.addEventListener("click", burn));
  $("#back").addEventListener("click", backToReview);
  $("#download").addEventListener("click", saveOutput);
  $("#mark-leaks").addEventListener("click", markLeaks);
  $("#show-text").addEventListener("click", showTextLayer);
  $("#text-close").addEventListener("click", () => { $("#text-modal").hidden = true; });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      $("#text-modal").hidden = true;
      if (S.drag) { const n = S.drag.page; S.drag = null; S.els[n].gTemp.textContent = ""; }
      if (document.activeElement === $("#search")) { $("#search").value = ""; runSearch(); $("#search").blur(); }
      return;
    }
    if (document.body.dataset.screen !== "review") return;
    if (e.target.matches("input, textarea")) return;
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "z") { e.preventDefault(); undo(); return; }
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const k = e.key.toLowerCase();
    if (k === "t") setTool("text");
    else if (k === "a") setTool("area");
    else if (k === "b") setMethod("blackout");
    else if (k === "s") setMethod("scramble");
    else if (k === "/") { e.preventDefault(); $("#search").focus(); }
  });

  // Closing the tab burns the document on the server too.
  window.addEventListener("pagehide", () => {
    if (S.id) navigator.sendBeacon(`/api/doc/${S.id}/burn`);
  });

  setTool("text");
  setMethod("blackout");
  setClass("unclass", "UNCLASSIFIED // NO DOCUMENT ON FILE");
}

wire();
