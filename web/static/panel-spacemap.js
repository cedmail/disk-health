// Space map: a squarified treemap of the latest folder-size snapshot (GET /api/spacemap).
// Two levels at once (folders with a header strip, their subfolders inside); click to zoom, Esc/Backspace to zoom out.
(() => {
  "use strict";
  const DH = window.DH;
  if (!DH) return;
  const { esc, hkb } = DH;
  const tilde = p => DH.tilde(p);
  const MIN_DELTA_KB = 50 * 1024;          // the scan threshold: smaller changes are noise
  const FULL_DELTA_KB = 20 * 1024 * 1024;  // 20 GB = full color

  const style = document.createElement("style");
  style.textContent = `
#spacemap .sm-wrap { display: grid; grid-template-columns: minmax(0, 1fr) 300px; gap: 20px; }
#spacemap .sm-col { position: relative; min-width: 0; }
#spacemap .crumbs { margin-bottom: 10px; min-height: 19px; }
#spacemap .crumbs a { color: var(--accent); }
#spacemap .sm-map { position: relative; height: 520px; border-radius: 10px; overflow: hidden; background: var(--surface-2); user-select: none; -webkit-user-select: none; }
#spacemap .sm-map.loading .sm-b { opacity: .55; transition: opacity .2s; }
#spacemap .sm-b { position: absolute; overflow: hidden; border-radius: 3px; cursor: pointer; color: var(--text); outline: none; }
#spacemap .sm-b.l1 { border-radius: 5px; }
#spacemap .sm-b.other { cursor: default; color: var(--text-2); background-image: repeating-linear-gradient(135deg, transparent 0 5px, color-mix(in srgb, var(--text) 6%, transparent) 5px 10px); }
#spacemap .sm-b.l2 { z-index: 1; }
#spacemap .sm-b:hover, #spacemap .sm-b.hl { box-shadow: inset 0 0 0 2px var(--text); }
#spacemap .sm-b.l2:hover, #spacemap .sm-b.l2.hl { z-index: 2; }
#spacemap .sm-b.other:hover { box-shadow: inset 0 0 0 1px var(--muted); }
#spacemap .sm-b:focus-visible { box-shadow: inset 0 0 0 2px var(--text), inset 0 0 0 4px var(--surface); }
#spacemap .sm-b.l2:focus-visible { z-index: 3; }
#spacemap .sm-lbl { position: absolute; left: 0; right: 0; top: 0; padding: 4px 6px 0; line-height: 1.25; pointer-events: none; }
#spacemap .sm-lbl .n, #spacemap .sm-lbl .s { display: block; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
#spacemap .sm-lbl .n { font-size: 12px; font-weight: 600; }
#spacemap .sm-lbl .s { font-size: 11.5px; font-variant-numeric: tabular-nums; opacity: .8; }
#spacemap .sm-b.l2 .sm-lbl { padding: 3px 5px 0; }
#spacemap .sm-b.l2 .sm-lbl .n { font-size: 11.5px; font-weight: 550; }
#spacemap .sm-b.l2 .sm-lbl .s { font-size: 11px; }
#spacemap .sm-b.hd .sm-lbl { display: flex; gap: 8px; align-items: baseline; padding: 3px 7px 0; height: 20px; }
#spacemap .sm-b.hd .sm-lbl .n { flex: 0 1 auto; min-width: 0; }
#spacemap .sm-b.hd .sm-lbl .s { flex: none; }
#spacemap .sm-tip { position: absolute; top: 0; left: 0; z-index: 10; pointer-events: none; background: var(--text); color: var(--bg); padding: 7px 10px; border-radius: 8px; font-size: 12px; line-height: 1.45; max-width: min(380px, calc(100% - 8px)); opacity: 0; transition: opacity .08s; box-shadow: 0 4px 18px rgba(0,0,0,.18); }
#spacemap .sm-tip.on { opacity: 1; }
#spacemap .sm-tip .p { font-family: var(--mono); font-size: 11.5px; overflow-wrap: anywhere; }
#spacemap .sm-tip .k { opacity: .7; }
#spacemap .sm-tip .up { color: color-mix(in srgb, var(--critical) 55%, var(--bg)); font-weight: 600; }
#spacemap .sm-tip .down { color: color-mix(in srgb, var(--good) 55%, var(--bg)); font-weight: 600; }
#spacemap .sm-tip .h { opacity: .6; font-size: 11px; margin-top: 2px; }
#spacemap .sm-legend { display: flex; align-items: center; gap: 8px 14px; flex-wrap: wrap; margin-top: 10px; font-size: 12px; color: var(--text-2); }
#spacemap .sm-legend .sw { display: inline-block; width: 12px; height: 12px; border-radius: 3px; vertical-align: -2px; margin-right: 5px; border: 1px solid color-mix(in srgb, var(--text) 10%, transparent); }
#spacemap .sm-legend .ramp { display: inline-block; width: 120px; height: 10px; border-radius: 3px; vertical-align: -1px; margin: 0 6px; }
#spacemap .sm-legend .striped { background: var(--surface-2); background-image: repeating-linear-gradient(135deg, transparent 0 3px, color-mix(in srgb, var(--text) 14%, transparent) 3px 6px); }
#spacemap .sm-side { min-width: 0; }
#spacemap .sm-cur { padding: 2px 0 12px; border-bottom: 1px solid var(--border); margin-bottom: 8px; }
#spacemap .sm-cur .t { font-family: var(--mono); font-size: 12.5px; font-weight: 600; overflow-wrap: anywhere; }
#spacemap .sm-cur .v { display: flex; align-items: baseline; gap: 10px; margin-top: 4px; flex-wrap: wrap; }
#spacemap .sm-cur .v b { font-size: 22px; font-weight: 650; letter-spacing: -.01em; }
#spacemap .sm-cur .hrow { display: flex; align-items: center; gap: 6px; min-height: 26px; }
#spacemap .sm-cur .hrow .t { flex: 1; min-width: 0; }
#spacemap .sm-h { font-size: 12px; font-weight: 600; color: var(--muted); margin: 4px 0 4px; }
#spacemap .sm-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 2px 10px; padding: 6px 6px; border-radius: 8px; cursor: pointer; align-items: baseline; }
#spacemap .sm-row:hover, #spacemap .sm-row.hl { background: var(--surface-2); }
#spacemap .sm-row:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
#spacemap .sm-row.other { cursor: default; }
#spacemap .sm-row .n { font-family: var(--mono); font-size: 12.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
#spacemap .sm-row.other .n { color: var(--muted); font-family: inherit; }
#spacemap .sm-row .s { font-size: 12.5px; text-align: right; }
#spacemap .sm-row .s .pc { color: var(--muted); margin-left: 6px; display: inline-block; min-width: 30px; }
#spacemap .sm-row .bar { grid-column: 1 / -1; height: 4px; }
#spacemap .sm-row .d { font-size: 11.5px; margin-left: 6px; }
#spacemap .sm-empty { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 10px; height: 100%; color: var(--muted); text-align: center; padding: 20px; }
#spacemap .sm-empty b { color: var(--text); font-weight: 600; }
#spacemap .sm-map.is-empty { height: 220px; }
@media (max-width: 980px) { #spacemap .sm-wrap { grid-template-columns: 1fr; } }
@media (max-width: 700px) { #spacemap .sm-map { height: 380px; } #spacemap .sm-map.is-empty { height: 220px; } }
`;
  document.head.appendChild(style);

  const S = { path: "", mode: "size", data: null, req: 0, lastScan: undefined, nodes: [], el: null, hover: -1, loading: false };

  // ---------------------------------------------------------------- squarified treemap (Bruls, Huizing & van Wijk)
  // items: [{kb}] sorted by kb desc. Returns [{item, x, y, w, h}] filling the rectangle.
  function squarify(items, x, y, w, h) {
    const out = [];
    const list = items.filter(i => i.kb > 0);
    const total = list.reduce((s, i) => s + i.kb, 0);
    if (!list.length || w <= 0 || h <= 0 || total <= 0) return out;
    const scale = (w * h) / total;
    const areas = list.map(i => i.kb * scale);
    const worst = (sum, max, min, side) => { const s2 = side * side, q = sum * sum; return Math.max(s2 * max / q, q / (s2 * min)); };
    let i = 0;
    while (i < list.length) {
      const side = Math.min(w, h);
      let j = i, sum = 0, max = 0, min = Infinity, best = Infinity;
      while (j < list.length) {   // grow the row while the worst aspect ratio improves
        const a = areas[j], ns = sum + a, nmax = Math.max(max, a), nmin = Math.min(min, a);
        const r = worst(ns, nmax, nmin, side);
        if (j > i && r > best) break;
        best = r; sum = ns; max = nmax; min = nmin; j++;
      }
      if (j === list.length) sum = areas.slice(i).reduce((s, a) => s + a, 0);
      if (w >= h) {               // lay the row out as a column on the left
        const cw = j === list.length ? w : sum / h; let cy = y;
        for (let k = i; k < j; k++) { const ch = k === j - 1 ? y + h - cy : areas[k] / cw; out.push({ item: list[k], x, y: cy, w: cw, h: ch }); cy += ch; }
        x += cw; w -= cw;
      } else {                    // ... or as a row on top
        const rh = j === list.length ? h : sum / w; let cx = x;
        for (let k = i; k < j; k++) { const cw = k === j - 1 ? x + w - cx : areas[k] / rh; out.push({ item: list[k], x: cx, y, w: cw, h: rh }); cx += cw; }
        y += rh; h -= rh;
      }
      i = j;
    }
    return out;
  }

  // ---------------------------------------------------------------- colors
  const mix = (c, pct, base = "var(--surface)") => `color-mix(in srgb, ${c} ${pct.toFixed(0)}%, ${base})`;
  function growthT(d) { const a = Math.abs(d); if (a < MIN_DELTA_KB) return 0;
    return Math.max(0, Math.min(1, Math.log(a / MIN_DELTA_KB) / Math.log(FULL_DELTA_KB / MIN_DELTA_KB))); }
  function fill(n, level, rank, count) {
    if (S.mode === "size") {
      if (n.other) return "var(--surface-2)";
      if (level === 1) return mix("var(--accent)", n.children && n.children.length ? 58 : 46);
      // subfolders: lighter, a touch stronger for the larger ones so neighbors separate
      return mix("var(--accent)", 34 - 14 * (count > 1 ? rank / (count - 1) : 0));
    }
    const d = n.delta_kb;
    if (d == null) return "var(--surface-2)";
    const t = growthT(d);
    const neutral = mix("var(--muted)", level === 1 ? 26 : 16);
    if (t === 0) return neutral;
    return mix(d > 0 ? "var(--critical)" : "var(--good)", 18 + 54 * t, neutral);
  }
  const deltaTxt = d => d == null ? "" : (d > 0 ? "+" : d < 0 ? "−" : "±") + hkb(Math.abs(d));
  const deltaCls = d => d == null || Math.abs(d) < MIN_DELTA_KB ? "" : d > 0 ? "up" : "down";

  // ---------------------------------------------------------------- data
  async function load(path, opts = {}) {
    const id = ++S.req;
    S.loading = true; if (S.el) S.el.map.classList.add("loading");
    const qs = new URLSearchParams({ depth: "2" });
    if (path) qs.set("path", path);
    if (S.mode !== "size") qs.set("compare_days", String(S.mode));
    let d;
    try { d = await DH.api("/api/spacemap?" + qs); }
    catch (e) { if (id === S.req) { S.loading = false; S.el.map.classList.remove("loading"); DH.toast("Space map: " + esc(e.message || e)); } return; }
    if (id !== S.req) return;
    S.loading = false; S.el.map.classList.remove("loading");
    if (d.ready && !d.found && path) {   // vanished from the latest scan: fall back to the roots
      DH.toast(`${esc(tilde(path))} isn't in the latest scan`);
      S.path = ""; return load("", opts);
    }
    S.path = path; S.data = d;
    draw(); if (opts.focus) focusFirst();
  }

  function zoom(path, kbd) { if (path === S.path) return; hideTip(); load(path, { focus: kbd }); }
  function zoomOut(kbd) {
    const d = S.data; if (!d || !d.path) return;
    const up = d.root && d.path !== d.root ? d.path.slice(0, d.path.lastIndexOf("/")) || "/" : "";
    hideTip(); load(up, { focus: kbd });
  }
  function focusFirst() { const b = S.el.map.querySelector(".sm-b[tabindex]"); if (b) b.focus({ preventScroll: true }); }

  // ---------------------------------------------------------------- drawing
  function draw() {
    const el = S.el, d = S.data;
    if (!d) return;
    drawCrumbs(); drawLegend(); drawSide();
    el.map.classList.toggle("is-empty", !d.ready);
    el.crumbs.style.display = d.ready ? "" : "none";
    if (!d.ready || !d.tree) {
      el.map.innerHTML = `<div class="sm-empty"><b>No folder sizes yet</b><span>Run a scan first: the map is drawn from the daily folder-size snapshot.</span><button class="small primary" data-sm-scan>Run a scan</button></div>`;
      el.sub.textContent = "";
      return;
    }
    el.sub.textContent = `latest scan ${DH.ago(d.ts)} · click to zoom`;
    const W = el.map.clientWidth, H = el.map.clientHeight;
    const nodes = S.nodes = [], html = [];
    const kids = d.tree.children || [];
    const push = (n, parent, level, r, cls, rank, count) => {
      const i = nodes.push({ n, parent, level }) - 1;
      const x = Math.round(r.x), y = Math.round(r.y), w = Math.round(r.x + r.w) - x, h = Math.round(r.y + r.h) - y;
      const bw = Math.max(0, w - 1), bh = Math.max(0, h - 1);  // 1px gap: the parent color shows through
      const focusable = !n.other && bw >= 14 && bh >= 12;
      let lbl = "";
      if (cls.includes("hd")) lbl = `<div class="sm-lbl"><span class="n">${esc(name(n))}</span>${bw > 110 ? `<span class="s">${hkb(n.kb)}</span>` : ""}</div>`;
      else if (bw >= 34 && bh >= 17) lbl = `<div class="sm-lbl"><span class="n">${esc(name(n))}</span>${bh >= 34 && bw >= 58 ? `<span class="s">${hkb(n.kb)}</span>` : ""}</div>`;
      html.push(`<div class="sm-b l${level} ${cls}${n.other ? " other" : ""}" data-i="${i}"${focusable ? ` tabindex="0" role="button" aria-label="${esc(name(n))}, ${hkb(n.kb)}${S.mode !== "size" && n.delta_kb != null ? ", " + deltaTxt(n.delta_kb) : ""}"` : ""} style="left:${x}px;top:${y}px;width:${bw}px;height:${bh}px;background-color:${fill(n, level, rank, count)}">${lbl}</div>`);
      return { x, y, w: bw, h: bh };
    };
    const top = squarify(kids, 0, 0, W + 1, H + 1);  // +1: the last gap falls outside the map
    top.forEach((r, ri) => {
      const n = r.item, ch = (n.children || []).filter(c => c.kb > 0);
      const nested = ch.length > 0 && !(ch.length === 1 && ch[0].other) && r.w >= 64 && r.h >= 52;
      const b = push(n, d.tree, 1, r, nested ? "hd" : "", ri, top.length);
      if (!nested) return;
      const pad = 3, head = 21;
      const inner = squarify(ch, b.x + pad, b.y + head, b.w - 2 * pad + 1, b.h - head - pad + 1);
      const real = inner.filter(c => !c.item.other).length;
      let k = 0;
      inner.forEach(c => push(c.item, n, 2, c, "", c.item.other ? 0 : k++, real));
    });
    el.map.innerHTML = html.join("") || `<div class="sm-empty"><span>Nothing above the scan threshold in here.</span></div>`;
  }

  function name(n) { return n.root || n.virtual ? tilde(n.path) || n.name : n.name; }
  function isRootPath(p) { return S.data && (S.data.roots || []).includes(p); }

  function drawCrumbs() {
    const d = S.data, el = S.el;
    const parts = [];
    if (!d || !d.path) parts.push('<span class="here">All roots</span>');
    else {
      parts.push('<a href="#spacemap" data-sm-go="">All roots</a>');
      const root = d.root || d.path;
      const steps = [[root, tilde(root)]];
      if (d.path !== root) { let acc = root.replace(/\/$/, "");
        for (const seg of d.path.slice(acc.length + 1).split("/")) { acc += "/" + seg; steps.push([acc, seg]); } }
      steps.forEach(([p, label], i) => parts.push(i === steps.length - 1 ? `<span class="here">${esc(label)}</span>` : `<a href="#spacemap" data-sm-go="${esc(p)}">${esc(label)}</a>`));
    }
    el.crumbs.innerHTML = parts.join(' <span class="dim">/</span> ');
  }

  function drawLegend() {
    const d = S.data, el = S.el;
    if (!d || !d.ready || !d.tree) { el.legend.innerHTML = ""; return; }
    if (S.mode === "size") {
      el.legend.innerHTML = `<span>Area = size.</span><span><i class="sw" style="background:${mix("var(--accent)", 58)}"></i>folder</span><span><i class="sw" style="background:${mix("var(--accent)", 28)}"></i>its subfolders</span><span><i class="sw striped"></i>smaller items (folders under 50 MB, files)</span>`;
      return;
    }
    const c = d.compare || {};
    if (!c.from_ts) { el.legend.innerHTML = `<span>Growth needs a second scan to compare with. Showing every folder as unknown (striped).</span>`; return; }
    const ramp = `linear-gradient(90deg, ${mix("var(--good)", 72, mix("var(--muted)", 16))}, ${mix("var(--muted)", 16)} 50%, ${mix("var(--critical)", 72, mix("var(--muted)", 16))})`;
    el.legend.innerHTML = `<span>Change since ${DH.fmtDate(c.from_ts)} (${c.window_days} days):</span><span>shrank<i class="ramp" style="background:${ramp}"></i>grew</span><span>gray = changed less than 50 MB; stronger color = more GB (full at 20 GB)</span><span><i class="sw striped"></i>no earlier data</span>`;
  }

  function drawSide() {
    const d = S.data, el = S.el;
    if (!d || !d.ready || !d.tree) { el.side.innerHTML = ""; return; }
    const t = d.tree, kids = (t.children || []).filter(c => c.kb > 0);
    const growth = S.mode !== "size" && d.compare && d.compare.from_ts;
    const top = kids.filter(c => !c.other).slice(0, 10);
    const other = kids.find(c => c.other);
    const max = Math.max(1, ...kids.map(c => c.kb));
    const row = (c, i) => {
      const pct = t.kb ? 100 * c.kb / t.kb : 0;
      const dl = growth && c.delta_kb != null && Math.abs(c.delta_kb) >= MIN_DELTA_KB ? `<span class="d ${c.delta_kb > 0 ? "delta-up" : "delta-down"}">${deltaTxt(c.delta_kb)}</span>` : "";
      return `<div class="sm-row${c.other ? " other" : ""}" data-r="${i}"${c.other ? "" : ` tabindex="0" role="button" title="${esc(c.path)}"`}>
        <span class="n">${esc(c.other ? (c.count ? `${c.count} more folders + smaller items` : "Smaller items") : name(c))}</span>
        <span class="s num">${hkb(c.kb)}<span class="pc">${pct < 1 ? "<1" : pct.toFixed(0)}%</span>${dl}</span>
        <div class="bar" style="width:${Math.max(0.5, 100 * c.kb / max).toFixed(1)}%;${c.other ? "background:var(--border)" : ""}"></div></div>`;
    };
    const rows = top.concat(other && kids.length > top.length ? [other] : []);
    S.sideRows = rows;
    const dTot = growth && t.delta_kb != null ? `<span class="${t.delta_kb > 0 ? "delta-up" : "delta-down"} num">${deltaTxt(t.delta_kb)} since ${DH.fmtDate(d.compare.from_ts)}</span>` : "";
    el.side.innerHTML = `<div class="sm-cur"><div class="hrow"><div class="t" title="${esc(t.path)}">${esc(t.virtual ? "All scan roots" : tilde(t.path))}</div>${t.virtual ? "" : `<button class="small" data-sm-reveal="${esc(t.path)}" title="Reveal in Finder">Finder</button>`}${d.path ? `<button class="small" data-sm-up title="Zoom out (Esc)">Up</button>` : ""}</div>
      <div class="v"><b class="num">${hkb(t.kb)}</b>${dTot}</div></div>
      <div class="sm-h">${rows.length ? `Largest ${t.virtual ? "roots" : "inside"}` : "No subfolders over 50 MB"}</div>${rows.map(row).join("")}`;
  }

  // ---------------------------------------------------------------- tooltip
  function showTip(i, ev) {
    const el = S.el, rec = S.nodes[i]; if (!rec) return;
    const n = rec.n, p = rec.parent;
    const pct = p && p.kb ? 100 * n.kb / p.kb : 0;
    let g = "";
    if (S.mode !== "size") {
      if (!S.data.compare || !S.data.compare.from_ts) g = `<div class="k">Growth: needs a second scan</div>`;
      else if (n.delta_kb == null) g = `<div class="k">Growth: unknown (not measured ${S.mode} days ago)</div>`;
      else g = `<div><span class="k">Last ${S.data.compare.window_days} days:</span> <span class="${deltaCls(n.delta_kb)}">${deltaTxt(n.delta_kb)}</span>${n.new ? ' <span class="k">(new, was under 50 MB)</span>' : ""}</div>`;
    }
    const pathTxt = n.other ? `${tilde(p.path)}: ${n.count ? n.count + " more folders and " : ""}folders under 50 MB + files` : tilde(n.path);
    el.tip.innerHTML = `<div class="p">${esc(pathTxt)}</div><div><b class="num">${hkb(n.kb)}</b> <span class="k">· ${pct < 0.1 ? "<0.1" : pct.toFixed(pct < 10 ? 1 : 0)}% of ${esc(p.virtual ? "all roots" : name(p))}</span></div>${g}${n.other ? "" : `<div class="h">Click to zoom · double-click to reveal in Finder</div>`}`;
    el.tip.classList.add("on");
    const col = el.col.getBoundingClientRect(), tw = el.tip.offsetWidth, th = el.tip.offsetHeight;
    let x, y;
    if (ev && ev.clientX != null) { x = ev.clientX - col.left + 14; y = ev.clientY - col.top + 16; }
    else { const b = ev.getBoundingClientRect(); x = b.left - col.left + 8; y = b.top - col.top + Math.min(b.height, 40); }
    if (x + tw > col.width - 4) x = Math.max(4, (ev && ev.clientX != null ? ev.clientX - col.left - 14 : col.width) - tw);
    if (y + th > col.height - 4) y = Math.max(4, (ev && ev.clientY != null ? ev.clientY - col.top - 12 : col.height) - th);
    el.tip.style.transform = `translate(${Math.round(x)}px, ${Math.round(y)}px)`;
  }
  function retip() {   // after a redraw, keep the tooltip on whatever block is now under the pointer
    if (!S.el.tip.classList.contains("on")) return;
    const m = S.mouse, t = m && document.elementFromPoint(m.clientX, m.clientY), b = t && t.closest("#spacemap .sm-b");
    if (b) showTip(+b.dataset.i, m); else hideTip();
  }
  function hideTip() { if (S.el) S.el.tip.classList.remove("on"); }

  async function reveal(path) {
    if (!path) return;
    try { await DH.api("/api/reveal", { path }); }
    catch (e) { DH.toast(`Couldn't reveal ${esc(tilde(path))}: ${esc(e.message || e)}`); }
  }

  function setMode(m) {
    if (S.mode === m) return;
    S.mode = m;
    S.el.seg.querySelectorAll("button").forEach(b => { const on = b.dataset.m === String(m); b.classList.toggle("on", on); b.setAttribute("aria-pressed", on); });
    load(S.path);
  }

  // ---------------------------------------------------------------- panel
  DH.registerPanel({
    id: "spacemap", title: "Space map", nav: "Map", before: "caches",
    init(el) {
      el.tools.innerHTML = `<div class="seg" role="group" aria-label="Color by"><button data-m="size" class="on" aria-pressed="true">Size</button><button data-m="7" aria-pressed="false">Growth 7d</button><button data-m="30" aria-pressed="false">Growth 30d</button></div>`;
      el.body.innerHTML = `<div class="sm-wrap"><div class="sm-col"><div class="crumbs"></div><div class="sm-map" role="group" aria-label="Space map: folders sized by disk usage. Enter zooms in, Escape zooms out."><div class="panel-loading">Loading…</div></div><div class="sm-legend"></div><div class="sm-tip" role="tooltip"></div></div><aside class="sm-side" aria-label="Largest items"></aside></div>`;
      Object.assign(el, { seg: el.tools.querySelector(".seg"), crumbs: el.body.querySelector(".crumbs"), map: el.body.querySelector(".sm-map"),
        legend: el.body.querySelector(".sm-legend"), tip: el.body.querySelector(".sm-tip"), side: el.body.querySelector(".sm-side"), col: el.body.querySelector(".sm-col") });
      S.el = el;
      el.seg.addEventListener("click", e => { const b = e.target.closest("button[data-m]"); if (b) setMode(b.dataset.m === "size" ? "size" : +b.dataset.m); });

      const blk = e => e.target.closest(".sm-b");
      let clickT = null;
      el.map.addEventListener("click", e => {
        if (e.target.closest("[data-sm-scan]")) return DH.run("scan");
        const b = blk(e); if (!b) return;
        const rec = S.nodes[+b.dataset.i]; if (!rec || rec.n.other) return;
        // wait a moment so a double-click (reveal) doesn't also zoom
        clearTimeout(clickT); if (e.detail > 1) return;
        clickT = setTimeout(() => zoom(rec.n.path), 230);
      });
      el.map.addEventListener("dblclick", e => { const b = blk(e); if (!b) return; clearTimeout(clickT);
        const rec = S.nodes[+b.dataset.i]; if (rec && !rec.n.other) reveal(rec.n.path); });
      el.map.addEventListener("mousemove", e => { S.mouse = { clientX: e.clientX, clientY: e.clientY };
        const b = blk(e); if (!b) { hideTip(); return; } showTip(+b.dataset.i, e); });
      el.map.addEventListener("mouseleave", () => { S.mouse = null; hideTip(); });
      el.map.addEventListener("focusin", e => { const b = blk(e); if (b) showTip(+b.dataset.i, b); });
      el.map.addEventListener("focusout", hideTip);

      el.side.addEventListener("click", e => {
        if (e.target.closest("[data-sm-reveal]")) return reveal(e.target.closest("[data-sm-reveal]").dataset.smReveal);
        if (e.target.closest("[data-sm-up]")) return zoomOut();
        const r = e.target.closest(".sm-row"); if (!r) return;
        const c = (S.sideRows || [])[+r.dataset.r]; if (c && !c.other) zoom(c.path);
      });
      el.side.addEventListener("mouseover", e => { const r = e.target.closest(".sm-row");
        el.map.querySelectorAll(".sm-b.hl").forEach(b => b.classList.remove("hl"));
        if (!r) return; const c = (S.sideRows || [])[+r.dataset.r];
        const i = S.nodes.findIndex(x => x.n === c); if (i >= 0) { const b = el.map.querySelector(`.sm-b[data-i="${i}"]`); if (b) b.classList.add("hl"); } });
      el.side.addEventListener("mouseleave", () => el.map.querySelectorAll(".sm-b.hl").forEach(b => b.classList.remove("hl")));
      el.crumbs.addEventListener("click", e => { const a = e.target.closest("[data-sm-go]"); if (!a) return; e.preventDefault(); zoom(a.dataset.smGo); });

      // Esc / Backspace zoom out while the map is "active": focus inside the card, or the last click was in it
      // (zooming re-renders the blocks, which drops focus to <body>).
      document.addEventListener("pointerdown", e => { S.active = el.section.contains(e.target); }, true);
      document.addEventListener("keydown", e => {
        const inside = el.section.contains(e.target);
        if (!inside && !(S.active && e.target === document.body)) return;
        if (inside && e.target.closest(".sm-b, .sm-row") && (e.key === "Enter" || e.key === " ")) { e.preventDefault();
          if (e.target.classList.contains("sm-b")) { const rec = S.nodes[+e.target.dataset.i]; if (rec && !rec.n.other) zoom(rec.n.path, true); }
          else { const c = (S.sideRows || [])[+e.target.dataset.r]; if (c && !c.other) zoom(c.path, true); }
          return; }
        if ((e.key === "Escape" || e.key === "Backspace") && !e.target.closest("input, textarea, select, dialog") && S.data && S.data.path) {
          e.preventDefault(); zoomOut(inside); }
      });

      let rt = null, lastW = 0;
      const ro = new ResizeObserver(() => { const w = el.map.clientWidth; if (w === lastW) return; lastW = w;
        clearTimeout(rt); rt = setTimeout(() => { draw(); retip(); }, 120); });
      ro.observe(el.map);
    },
    render(data, D) {
      // Re-fetch only when a new scan landed (the dashboard refreshes every minute).
      if (S.lastScan === D.last_scan && (S.data || S.loading)) return;
      S.lastScan = D.last_scan;
      load(S.path);
    },
  });
})();
