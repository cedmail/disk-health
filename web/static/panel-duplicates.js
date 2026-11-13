// Duplicate files panel (server: diskhealth/features/duplicates.py)
(() => {
  const PAGE = 50;
  let shown = PAGE, last = {}, pel = null;
  const { esc, hb, ago, tilde } = DH;
  const plural = (n, w) => `${n.toLocaleString()} ${w}${n === 1 ? "" : "s"}`;
  const fdate = ts => ts ? new Date(ts * 1000).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }) : "";

  function fileRow(g, f, i) {
    const t = tilde(f.path), k = t.lastIndexOf("/");
    const last = g.files.length < 2;
    return `<tr><td class="pcell"><div class="pc" title="${esc(f.path)}"><div class="leaf">${esc(t.slice(k + 1))}</div><div class="parent"><bdi dir="ltr">${esc(t.slice(0, k + 1))}</bdi></div><div class="tags">${i === 0 ? `<span class="tag good">newest</span>` : ""}${f.links ? `<span class="tag warn" title="Trashing this name frees nothing while another hard link exists">${f.links} hard links</span>` : ""}<span class="note m-only">${esc(fdate(f.mtime))} · ${esc(hb(g.size))}</span></div></div></td>
      <td class="num note d-only">${esc(fdate(f.mtime))}</td><td class="r num d-only">${esc(hb(g.size))}</td>
      <td><div class="actions"><button class="small d-only" data-dupreveal="${esc(f.path)}" title="Reveal in Finder">Finder</button><button class="small danger" data-duptrash="${esc(f.path)}" ${last ? "disabled title=\"Last copy\"" : ""}>Move to Trash</button></div></td></tr>`;
  }

  function groupRows(g) {
    const n = g.files.length;
    return `<tr class="group-h"><td colspan="3" class="d-only">${n} copies · ${esc(hb(g.size))} each · <b>${esc(hb(g.wasted))}</b> wasted</td><td class="dup-m">${n} copies · <b>${esc(hb(g.wasted))}</b> wasted</td>
      <td><div class="actions"><button class="small" data-dupkeep="${esc(g.id)}" ${n < 2 ? "disabled" : ""} title="Move every copy but the newest to the Trash">Keep newest</button></div></td></tr>`
      + g.files.map((f, i) => fileRow(g, f, i)).join("");
  }

  DH.registerPanel({
    id: "duplicates", title: "Duplicate files", nav: "Duplicates",
    init(el) {
      // Mobile hides the date/size columns: the group header then spans one column instead of three.
      const st = document.createElement("style");
      st.textContent = "#duplicates td.dup-m { display: none; } @media (max-width: 700px) { #duplicates td.dup-m { display: table-cell; } }";
      document.head.appendChild(st);
      el.tools.innerHTML = `<button class="small primary" data-dupfind>Find duplicates</button>`;
      el.section.addEventListener("click", async e => {
        const b = e.target.closest("button"); if (!b || b.disabled) return;
        if (b.hasAttribute("data-dupfind")) { DH.run("duplicates-find"); return; }
        if (b.hasAttribute("data-dupmore")) { shown += PAGE * 4; render(); return; }
        if (b.dataset.dupreveal) { DH.api("/api/reveal", { path: b.dataset.dupreveal }).catch(err => DH.toast("Couldn't reveal: " + esc(err.message || err))); return; }
        if (b.dataset.duptrash) {
          const p = b.dataset.duptrash, g = (last.groups || []).find(x => x.files.some(f => f.path === p)); if (!g) return;
          const others = g.files.filter(f => f.path !== p);
          if (await DH.confirmBox("Move this copy to the Trash?", `<ul><li>${esc(tilde(p))}</li></ul><p>Frees <b>${esc(hb(g.size))}</b> once the Trash is emptied. ${others.length === 1 ? "This copy stays:" : `These ${others.length} copies stay:`}</p><ul>${others.map(f => `<li>${esc(tilde(f.path))}</li>`).join("")}</ul><p class="note">The other copies are re-checked (same content) first; the last copy is never trashed.</p>`, "Move to Trash", true))
            DH.run("duplicates-trash", p);
          return;
        }
        if (b.dataset.dupkeep) {
          const g = (last.groups || []).find(x => x.id === b.dataset.dupkeep); if (!g) return;
          const newest = g.files.reduce((a, f) => f.mtime > a.mtime ? f : a, g.files[0]);
          const rest = g.files.filter(f => f !== newest);
          if (await DH.confirmBox(`Keep only the newest copy?`, `<p>Keeps <code>${esc(tilde(newest.path))}</code> (${esc(fdate(newest.mtime))}) and moves ${rest.length === 1 ? "this copy" : `these ${rest.length} copies`} to the Trash, freeing <b>${esc(hb(g.size * rest.length))}</b>:</p><ul>${rest.map(f => `<li>${esc(tilde(f.path))}</li>`).join("")}</ul><p class="note">Every copy is re-checked (same content) first.</p>`, "Move to Trash", true))
            DH.run("duplicates-keep-newest", g.id);
        }
      });
    },
    render(data, D, el) { last = data || {}; pel = el; render(); },
  });

  function render() {
    if (!pel) return;
    const { sub, body } = pel, d = last;
    if (!d.ts) {
      sub.textContent = "never searched";
      body.innerHTML = `<div class="empty">Finds identical files in ${esc(((DH.D && DH.D.config && DH.D.config.duplicate_roots) || ["~/Downloads", "~/Documents", "~/Desktop"]).map(tilde).join(", "))}. Click <b>Find duplicates</b> (reads the files, takes a while the first time).</div>`;
      return;
    }
    const groups = d.groups || [];
    sub.textContent = `${plural(d.group_count || 0, "group")} · ${hb(d.total_wasted || 0)} wasted · searched ${ago(d.ts)}`;
    const roots = (d.roots || []).map(tilde);
    let html = `<div class="brew-grid">
      <div class="mini"><div class="label">Wasted space</div><div class="value num">${esc(hb(d.total_wasted || 0))}</div><div class="note">extra copies only</div></div>
      <div class="mini"><div class="label">Groups of identical files</div><div class="value num">${(d.group_count || 0).toLocaleString()}</div><div class="note">${(d.files_scanned || 0).toLocaleString()} files ≥ ${esc(d.min_mb)} MB compared</div></div>
      <div class="mini"><div class="label">Last search</div><div class="value">${esc(ago(d.ts))}</div><div class="note">took ${d.duration >= 1 ? esc(DH.dur(d.duration)) : "under a second"}</div></div>
      <div class="mini"><div class="label">Folders searched</div><ul>${roots.map(r => `<li class="path">${esc(r)}</li>`).join("")}</ul></div></div>`;
    if (!groups.length) { body.innerHTML = html + `<div class="empty">No duplicates found.</div>`; return; }
    const vis = groups.slice(0, shown);
    html += DH.tbl(`<thead><tr><th>File</th><th class="d-only">Modified</th><th class="r d-only">Size</th><th></th></tr></thead><tbody>${vis.map(groupRows).join("")}</tbody>`, "fit");
    if (groups.length > shown) html += `<div class="more"><button class="small" data-dupmore>Show more (${(groups.length - shown).toLocaleString()} more groups)</button></div>`;
    if ((d.group_count || 0) > groups.length) html += `<p class="note">Showing the ${groups.length} groups wasting the most space out of ${d.group_count.toLocaleString()}.</p>`;
    body.innerHTML = html;
  }
})();
