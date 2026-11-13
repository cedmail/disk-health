// Hidden space & big folders panel (diskhealth/features/storage.py)
(() => {
  const { esc, hkb, hb, ago, tilde, tbl } = DH;
  const open = new Set();   // expanded folder ids (survive the periodic re-render)
  let last = null;
  const day = ts => ts ? new Date(ts * 1000).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }) : "—";
  const snapLabel = s => s.ts ? new Date(s.ts * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : s.date;

  function hiddenHtml(h, err) {
    const snaps = h.snapshots || [];
    const vols = (h.volumes || []).slice(0, 6);
    const used = h.container_size ? h.container_size - h.container_free : 0;
    let html = `<div class="brew-grid">
      <div class="mini"><div class="label">APFS container</div><div class="value num">${h.container_size ? hb(h.container_size) : "—"}</div>
        <div class="note">${h.container_size ? `${hb(used)} used · ${hb(h.container_free)} free` : h.diskutil ? "diskutil gave no container info" : "diskutil not available"}</div></div>
      <div class="mini"><div class="label">Free for your files</div><div class="value num">${hb(h.volume_free || 0)}</div>
        <div class="note">of ${hb(h.volume_total || 0)}${h.purgeable_hint ? ` · ~${hb(h.purgeable_hint)} purgeable` : ""}</div></div>
      <div class="mini"><div class="label">Time Machine local snapshots</div><div class="value num">${h.tmutil === false ? "—" : snaps.length}</div>
        <div class="note">${snaps.length ? `${esc(day(h.oldest))} → ${esc(day(h.newest))} · shown as “purgeable”` : h.tmutil === false ? "tmutil not available" : "none on this disk"}</div></div>
      <div class="mini"><div class="label">Volumes in the container</div>${vols.length ? `<ul>${vols.map(v => `<li>${esc(v.name)} <span class="dim">${hb(v.used)}${(v.roles || []).length ? " · " + esc(v.roles.join(", ")) : ""}</span></li>`).join("")}</ul>` : `<div class="note" style="margin-top:6px">not available</div>`}</div>
    </div>`;
    if (err) html += `<div class="alert warning" style="margin-bottom:12px"><span class="lvl">▲ Admin needed</span><span class="msg">${esc(err.message || "")}</span><button class="small" data-st-copy="${esc(err.cmd)}">Copy command</button></div>`;
    if (snaps.length) html += tbl(`<thead><tr><th>Local snapshot</th><th>Age</th><th class="d-only">Name</th><th></th></tr></thead><tbody>${
      snaps.slice().reverse().map(s => `<tr><td class="num">${esc(snapLabel(s))}</td><td class="note" style="white-space:nowrap">${ago(s.ts)}</td>
        <td class="d-only"><span class="path">${esc(s.name)}</span></td>
        <td><div class="actions"><button class="small danger" data-st-snap="${esc(s.date)}">Delete</button></div></td></tr>`).join("")}</tbody>`, "fit")
      + `<p class="note" style="margin:8px 0 0">macOS doesn't report the size of each snapshot without root. Their space is “purgeable”: macOS frees it automatically when it needs room, and <b>Thin snapshots</b> asks it to do so now.</p>`;
    return html;
  }

  function childRows(f) {
    if (f.state === "fda") return "";
    const ch = f.children || [];
    if (!ch.length) return `<div class="note" style="padding:6px 0">Nothing inside.</div>`;
    const max = Math.max(...ch.map(c => c.kb), 1);
    return tbl(`<tbody>${ch.slice(0, 200).map(c => `<tr><td><div>${esc(c.name)} ${(c.tags || []).map(t => `<span class="tag warn">${esc(t)}</span>`).join(" ")}</div>
        ${c.detail ? `<div class="note">${esc(c.detail)}</div>` : ""}${c.path && c.path.startsWith("/") && !c.id ? `<div class="note path d-only">${esc(tilde(c.path))}</div>` : ""}</td>
      <td class="note" style="white-space:nowrap">${c.ts ? esc(day(c.ts)) : ""}</td>
      <td class="r num">${hkb(c.kb)}</td><td class="bar-cell"><div class="bar" style="width:${(100 * c.kb / max).toFixed(1)}%"></div></td>
      <td><div class="actions">${c.action ? `<button class="small danger" data-st-child="${esc(f.id)}" data-path="${esc(c.id || c.path)}">${c.action === "storage-simctl-runtime-delete" ? "Delete" : "Trash"}</button>` : ""}</div></td></tr>`).join("")}</tbody>`, "fit")
      + (ch.length > 200 ? `<div class="note">… ${ch.length - 200} more</div>` : "");
  }

  function foldersHtml(folders, fdaNote) {
    if (!folders.length) return `<div class="empty">None of these folders exist on this Mac.</div>`;
    const max = Math.max(...folders.map(f => f.kb), 1);
    return tbl(`<thead><tr><th>Folder</th><th class="r">Size</th><th class="bar-cell"></th><th class="d-only">Items</th><th class="d-only">Newest</th><th></th></tr></thead><tbody>${
      folders.map(f => { const fda = f.state === "fda", isOpen = open.has(f.id);
        return `<tr><td><div><b>${esc(f.title)}</b>${f.info_only ? ` <span class="tag">info</span>` : ""}${fda ? ` <span class="tag warn">Needs Full Disk Access</span>` : ""}${!f.complete && !fda ? ` <span class="tag warn" title="Some subfolders couldn't be read">partial</span>` : ""}</div>
          <div class="note path" title="${esc(f.path)}">${esc(tilde(f.path))}</div>
          ${fda ? `<div class="note">${esc(fdaNote)}</div>` : ""}</td>
        <td class="r num">${fda ? `<span class="note">?</span>` : hkb(f.kb)}</td>
        <td class="bar-cell">${fda ? "" : `<div class="bar" style="width:${(100 * f.kb / max).toFixed(1)}%"></div>`}</td>
        <td class="d-only num">${fda || !f.count ? "" : f.count.toLocaleString()}</td>
        <td class="d-only note" style="white-space:nowrap">${f.newest ? esc(day(f.newest)) : ""}${f.oldest && f.oldest !== f.newest ? `<div>oldest ${esc(day(f.oldest))}</div>` : ""}</td>
        <td><div class="actions wrap-m">${fda ? "" : `<button class="small" data-st-toggle="${esc(f.id)}" aria-expanded="${isOpen}">${isOpen ? "Hide" : "Details"}</button>`}
          ${f.path.startsWith("/Library") ? "" : `<button class="small" data-st-reveal="${esc(f.path)}" title="Open in Finder"><span class="d-only">Open in Finder</span><span class="m-only">Finder</span></button>`}</div></td></tr>
        ${isOpen ? `<tr><td colspan="6" style="background:var(--surface-2);border-radius:8px">
          <p class="note" style="margin:2px 0 8px">${esc(f.note)}</p>
          ${(f.actions || []).length ? `<div class="actions" style="justify-content:flex-start;margin-bottom:8px">${f.actions.map(a => `<button class="small danger" data-st-act="${esc(a.action)}" data-fid="${esc(f.id)}">${esc(a.label)}</button>`).join("")}</div>` : ""}
          ${childRows(f)}</td></tr>` : ""}`; }).join("")}</tbody>`, "fit");
  }

  // ---------------------------------------------------------------- Spotlight index (diskhealth/features/spotlight.py)
  const KIND = { git: "Git", docker: "Docker", build: "Build cache", toolchain: "Toolchain" };
  function spotlightHtml(sp) {
    if (!sp || !sp.ts) return `<div class="card-head" style="margin:22px 0 8px"><h2 style="font-size:14px">Spotlight index</h2></div>
      <div class="empty" style="padding:12px 0">Not checked yet. <button class="small" data-sp-refresh>Check now</button></div>`;
    const todo = (sp.suggestions || []).filter(x => !x.excluded), done = (sp.suggestions || []).filter(x => x.excluded);
    const status = sp.enabled === true ? `<span class="tag good">✓ indexing on</span>` : sp.enabled === false ? `<span class="tag warn">indexing off</span>` : `<span class="tag">status unknown</span>`;
    let html = `<div class="card-head" style="margin:22px 0 10px"><h2 style="font-size:14px">Spotlight index</h2>
        <span class="sub">${status} · suggestions checked ${ago(sp.ts)}</span><span class="spacer"></span>
        <button class="small" data-sp-refresh>Re-check</button></div>
      <div class="brew-grid">
        <div class="mini"><div class="label">System index</div><div class="value num">${sp.index_kb != null ? (sp.partial ? "≥ " : "") + hkb(sp.index_kb) : "protected"}</div>
          <div class="note">${sp.index_kb != null ? `read ${ago(sp.admin_ts)}` : `macOS hides its size, even from admins. To see it, give Terminal Full Disk Access, then <a href="#" data-sp-copy="sudo du -sh /System/Volumes/Data/.Spotlight-V100">copy the command</a>`}</div></div>
        <div class="mini"><div class="label">Apps' Spotlight data</div><div class="value num">${sp.user_index_kb != null ? hkb(sp.user_index_kb) : "—"}</div>
          <div class="note">~/Library/Metadata/CoreSpotlight (Mail, Messages, Notes… search data)</div></div>
        <div class="mini"><div class="label">Suggested to exclude</div><div class="value num">${todo.length}</div>
          <div class="note">${(sp.indexed_total || 0).toLocaleString()} indexed items in them</div></div>
        <div class="mini"><div class="label">After excluding</div><div class="value" style="font-size:15px;margin-top:4px"><button class="small danger" data-sp-rebuild>Rebuild index…</button></div>
          <div class="note">erases &amp; re-indexes so the excluded folders' entries are dropped</div></div>
      </div>`;
    if (!todo.length) html += `<div class="empty" style="padding:10px 0">Nothing to suggest: no big developer folder is being indexed.</div>`;
    else html += tbl(`<thead><tr><th>Folder</th><th class="r">Indexed items</th><th class="r d-only">Size</th><th></th></tr></thead><tbody>${
      todo.map(c => `<tr><td><div><b>${esc(c.reason)}</b> <span class="tag">${esc(KIND[c.kind] || c.kind)}</span></div>
          <div class="note path">${esc(tilde(c.path))}</div><div class="note">${esc(c.why)}</div></td>
        <td class="r num">${c.indexed == null ? "?" : c.indexed.toLocaleString()}</td>
        <td class="r num d-only">${c.kb != null ? hkb(c.kb) : ""}</td>
        <td><div class="actions"><button class="small primary" data-sp-exclude="${esc(c.path)}" title="Copies the path, shows the folder in Finder and opens Spotlight settings">Exclude…</button></div></td></tr>`).join("")}</tbody>`, "fit");
    if (done.length) html += `<p class="note" style="margin:8px 0 0">✓ Already excluded (not indexed): ${done.map(c => esc(tilde(c.path))).join(", ")}</p>`;
    html += `<p class="note" style="margin:8px 0 0">macOS has no command to add a Privacy exclusion, so <b>Exclude…</b> does the steps for you: it copies the path, reveals the folder in Finder and opens Spotlight settings — click <b>Search Privacy…</b> and drag the folder in (or click <b>+</b>, press <b>⌘⇧G</b> and paste). Excluded folders disappear from Spotlight/Finder search; your IDE and git grep are unaffected. Keep indexing on for the disk itself: the Apps panel reads “last opened” dates from Spotlight.</p>`;
    return html;
  }

  async function onClick(e, el) {
    const sp = e.target.closest("[data-sp-refresh],[data-sp-inspect],[data-sp-rebuild],[data-sp-exclude],[data-sp-copy]");
    if (sp) { e.preventDefault();
      if (sp.hasAttribute("data-sp-refresh")) return DH.run("spotlight-scan");
      if (sp.hasAttribute("data-sp-inspect")) { DH.toast("macOS will ask for your admin password…"); return DH.run("spotlight-inspect"); }
      if (sp.dataset.spCopy) { DH.toast(await DH.copyText(sp.dataset.spCopy) ? `Copied <code>${esc(sp.dataset.spCopy)}</code>` : "Couldn't access the clipboard"); return; }
      if (sp.hasAttribute("data-sp-rebuild")) {
        if (await DH.confirmBox("Rebuild the Spotlight index?", `<p>Runs <code>sudo mdutil -E /</code> (macOS asks for your admin password). The index is erased and rebuilt in the background: searches are incomplete and <code>mds_stores</code> uses CPU for a while (from under an hour to a few hours).</p><p class="note">Do this after excluding folders, or when the index is unusually big.</p>`, "Rebuild", true))
          DH.run("spotlight-rebuild");
        return; }
      if (sp.dataset.spExclude) { const p = sp.dataset.spExclude;
        const copied = await DH.copyText(p);
        try { await DH.api("/api/reveal", { path: p }); } catch (err) {}
        DH.run("spotlight-open-settings");
        DH.toast(`${copied ? "Path copied. " : ""}In Spotlight settings: <b>Search Privacy…</b> → drag <code>${esc(tilde(p))}</code> from Finder (or <b>+</b> then <b>⌘⇧G</b> and paste). Then click <b>Re-check</b>.`);
        return; }
    }
    const t = e.target.closest("button"); if (!t || !last) return;
    const folders = last.folders || [];
    if (t.dataset.stToggle) { const id = t.dataset.stToggle; open.has(id) ? open.delete(id) : open.add(id); render(last, el); return; }
    if (t.dataset.stReveal) { try { await DH.api("/api/reveal", { path: t.dataset.stReveal }); } catch (err) { DH.toast("Couldn't open Finder: " + esc(err.message)); } return; }
    if (t.dataset.stCopy) { DH.toast(await DH.copyText(t.dataset.stCopy) ? `Copied <code>${esc(t.dataset.stCopy)}</code> — paste it in Terminal` : "Couldn't access the clipboard"); return; }
    if (t.hasAttribute("data-st-thin")) {
      if (await DH.confirmBox("Thin Time Machine local snapshots?", `<p>Asks macOS to free as much snapshot space as it can now (<code>tmutil thinlocalsnapshots / 999999999999 4</code>). No admin rights needed.</p><p class="note">Local snapshots let Time Machine restore recent files while the backup disk is away; the next backup creates a new one.</p>`, "Thin snapshots"))
        DH.run("storage-thin-snapshots");
      return; }
    if (t.dataset.stSnap) { const d = t.dataset.stSnap;
      if (await DH.confirmBox(`Delete snapshot ${d}?`, `<p>Runs <code>tmutil deletelocalsnapshots ${esc(d)}</code>. Files can no longer be restored from this point in time (your Time Machine backups on the backup disk are not affected).</p><p class="note">If macOS requires admin rights, you'll get a command to copy and run in Terminal yourself.</p>`, "Delete snapshot", true))
        DH.run("storage-delete-snapshot", d);
      return; }
    if (t.dataset.stAct) { const f = folders.find(x => x.id === t.dataset.fid) || {};
      const a = t.dataset.stAct;
      const msg = a === "storage-simctl-delete-unavailable"
        ? `<p>Runs <code>xcrun simctl delete unavailable</code>: removes ${f.unavailable || 0} simulator device(s) whose runtime is gone (${hkb(f.unavailable_kb || 0)}). Not recoverable.</p>`
        : `<p>Moves every file in <code>${esc(tilde(f.path || ""))}</code> (${hkb(f.kb || 0)}) to the Trash. These are copies of attachments you opened; the originals stay in Mail.</p>`;
      if (await DH.confirmBox(a === "storage-simctl-delete-unavailable" ? "Delete unavailable simulators?" : "Move Mail Downloads to the Trash?", msg, a === "storage-simctl-delete-unavailable" ? "Delete" : "Move to Trash", true)) DH.run(a);
      return; }
    if (t.dataset.stChild) { const f = folders.find(x => x.id === t.dataset.stChild) || {};
      const c = (f.children || []).find(x => (x.id || x.path) === t.dataset.path); if (!c) return;
      let title, body, ok;
      if (c.action === "storage-trash-backup") {
        title = `Move the backup of ${c.name} to the Trash?`; ok = "Move to Trash";
        body = `<p>${hkb(c.kb)} · last backup ${esc(day(c.ts))} (${ago(c.ts)})${c.detail ? ` · ${esc(c.detail)}` : ""}</p><ul><li>${esc(c.path)}</li></ul><p>Only do this if the device is backed up elsewhere (iCloud or a newer backup). You can put it back from the Trash panel until you empty the Trash.</p>`;
      } else if (c.action === "storage-trash-archive") {
        title = `Move archive ${c.name} to the Trash?`; ok = "Move to Trash";
        body = `<p>${hkb(c.kb)} · ${esc(day(c.ts))}${c.detail ? ` · ${esc(c.detail)}` : ""}</p><ul><li>${esc(c.path)}</li></ul><p class="note">Its dSYMs are needed to symbolicate crash reports of that build.</p>`;
      } else {
        title = `Delete simulator runtime ${c.name}?`; ok = "Delete";
        body = `<p>Runs <code>xcrun simctl runtime delete ${esc(c.id)}</code> (${hkb(c.kb)}). Xcode can download it again (Settings ▸ Platforms).</p>`;
      }
      if (await DH.confirmBox(title, body, ok, true)) DH.run(c.action, c.id || c.path);
    }
  }

  function render(data, el) {
    last = data;
    if (!data.hidden) { el.sub.textContent = ""; el.body.innerHTML = `<div class="empty">Not scanned yet. Click <b>Refresh</b> (or wait for the daily scan).</div>`; return; }
    const folders = data.folders || [];
    el.sub.textContent = `${hkb(data.total_kb || 0)} in ${folders.length} folders · checked ${ago(data.ts)}`;
    const snaps = data.hidden.snapshots || [];
    el.body.innerHTML = `<div class="card-head" style="margin:0 0 10px"><h2 style="font-size:14px">Hidden space</h2><span class="sub">APFS container &amp; Time Machine local snapshots</span><span class="spacer"></span>
        ${snaps.length ? `<button class="small" data-st-thin>Thin snapshots</button>` : ""}</div>
      ${hiddenHtml(data.hidden, data.last_error)}
      <div class="card-head" style="margin:22px 0 8px"><h2 style="font-size:14px">Big folders</h2><span class="sub">backups, Xcode, Mail, Messages, Photos, iCloud Drive</span></div>
      ${(data.fda_needed || []).length ? `<div class="alert notice" style="margin-bottom:10px"><span class="lvl">● Full Disk Access</span><span class="msg">${esc(data.fda_needed.join(", "))} can't be measured: ${esc(data.fda_note)}</span></div>` : ""}
      ${foldersHtml(folders, data.fda_note || "")}
      ${spotlightHtml(((DH.D || {}).features || {}).spotlight)}`;
  }

  DH.registerPanel({
    id: "storage", title: "Hidden space & big folders", nav: "Storage", before: "trash",
    init(el) {
      el.tools.innerHTML = `<button class="small" data-st-refresh>Refresh</button>`;
      el.tools.addEventListener("click", e => { if (e.target.closest("[data-st-refresh]")) DH.run("storage-scan"); });
      el.body.addEventListener("click", e => onClick(e, el));
    },
    render(data, D, el) { render(data, el); },
  });
})();
