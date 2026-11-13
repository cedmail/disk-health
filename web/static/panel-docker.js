// Docker detail panel: VM disk, images, containers, volumes and build cache (server: diskhealth/features/docker.py)
(() => {
  const { esc, hb, ago, tilde, confirmBox, run } = DH;
  const st = { tab: "images", imgFilter: "all", sort: { images: ["size", -1], containers: ["size", -1], volumes: ["size", -1], build: ["size", -1] } };
  let last = null, el = null;

  const css = document.createElement("style");
  css.textContent = `
#docker .dk-grid { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 12px; margin-bottom: 14px; }
@media (max-width: 1100px) { #docker .dk-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
@media (max-width: 700px) { #docker .dk-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; } }
#docker .mini .value { white-space: nowrap; }
#docker .mini .sub2 { font-size: 12px; color: var(--muted); margin-top: 2px; }
#docker .dk-bulk { display: flex; flex-wrap: wrap; gap: 8px; margin: 0 0 14px; align-items: center; }
#docker .dk-bulk .lbl { font-size: 12.5px; color: var(--text-2); font-weight: 600; margin-right: 2px; }
#docker .dk-hint { margin: 0 0 14px; padding: 9px 12px; border-radius: 9px; background: var(--surface-2); border-left: 3px solid var(--warning); font-size: 13px; color: var(--text-2); }
#docker .dk-tabs { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-bottom: 8px; }
#docker .dk-tabs .seg { flex-wrap: wrap; }
#docker .dk-name { font-family: var(--mono); font-size: 12.5px; overflow-wrap: anywhere; }
#docker .dk-name b { font-weight: 600; }
#docker .dk-tags { margin-top: 3px; display: flex; gap: 4px; flex-wrap: wrap; }
#docker th.sort { cursor: pointer; user-select: none; }
#docker th.sort:hover { color: var(--text); }
#docker th.sort.on { color: var(--text); }
#docker td.dk-n { width: 100%; }
#docker .tag.run { color: var(--good-ink); }
@media (max-width: 700px) { #docker .mini { padding: 10px 12px; } #docker .mini .value { font-size: 18px; } }
`;
  document.head.appendChild(css);

  const sizeOf = i => (i.unique_size != null ? i.unique_size : i.size) || 0;
  const tag = (t, cls = "", title = "") => `<span class="tag ${cls}"${title ? ` title="${esc(title)}"` : ""}>${esc(t)}</span>`;
  const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
  const shortVol = n => /^[0-9a-f]{64}$/.test(n) ? n.slice(0, 12) + "…" : n;
  const GB = 1e9;

  function sortRows(rows, tab, keyFns) {
    const [k, dir] = st.sort[tab]; const f = keyFns[k] || keyFns.size;
    return [...rows].sort((a, b) => { const x = f(a), y = f(b);
      return (typeof x === "string" ? x.localeCompare(y) : (x || 0) - (y || 0)) * dir; });
  }
  function th(tab, key, label, cls = "") {
    const [k, dir] = st.sort[tab]; const on = k === key;
    return `<th class="sort ${on ? "on" : ""} ${cls}" data-sort="${key}" aria-sort="${on ? (dir < 0 ? "descending" : "ascending") : "none"}" tabindex="0">${label}${on ? (dir < 0 ? " ↓" : " ↑") : ""}</th>`;
  }

  // ---------------------------------------------------------------- header
  function minis(d) {
    const t = d.totals, vm = d.vm_disk;
    const m = (label, value, sub) => `<div class="mini"><div class="label">${label}</div><div class="value num">${value}</div><div class="sub2">${sub}</div></div>`;
    return `<div class="dk-grid">
      ${m(`VM disk · ${esc(d.engine || "Docker")}`, vm ? hb(vm.allocated) : "—", vm ? `on disk · ${hb(vm.apparent)} max` : "disk image not found")}
      ${m("Images", hb(t.images.size), `${plural(t.images.count, "image")} · <b>${hb(t.images.reclaimable)}</b> unused`)}
      ${m("Containers", `${t.containers.running}<span class="dim" style="font-size:14px;font-weight:500"> running</span>`, `${t.containers.stopped} stopped${t.containers.reclaimable ? ` · ${hb(t.containers.reclaimable)}` : ""}`)}
      ${m("Volumes", hb(t.volumes.size), `${plural(t.volumes.count, "volume")} · ${t.volumes.unused_count} unused (${hb(t.volumes.unused_size)})`)}
      ${m("Build cache", hb(t.build_cache.size), `${hb(t.build_cache.reclaimable)} reclaimable`)}
    </div>`;
  }

  function hint(d) {
    const vm = d.vm_disk; if (!vm || d.engine !== "Docker Desktop" && vm.engine !== "Docker Desktop") return "";
    if (vm.allocated < d.used * 1.3 + 2 * GB) return "";
    return `<div class="dk-hint">Docker Desktop's disk image takes <b>${hb(vm.allocated)}</b> on your Mac, but Docker reports only <b>${hb(d.used)}</b> in use.
      Docker Desktop releases the space back to macOS gradually; restart Docker Desktop if it doesn't.</div>`;
  }

  function bulk(d) {
    const t = d.totals.images, c = d.totals.containers, v = d.totals.volumes, b = d.totals.build_cache;
    const btn = (act, label, bytes, enabled) => `<button class="small${enabled ? "" : ""}" data-dk-bulk="${act}" ${enabled ? "" : "disabled"}>${label}${bytes ? ` <span class="dim num">${hb(bytes)}</span>` : ""}</button>`;
    return `<div class="dk-bulk"><span class="lbl">Clean up</span>
      ${btn("dangling", "Dangling images", t.dangling_size, t.dangling_count > 0)}
      ${btn("unused-images", "Unused images", t.reclaimable, t.unused_count > 0)}
      ${btn("stopped", "Stopped containers", c.reclaimable, c.stopped > 0)}
      ${btn("volumes", "Unused volumes", v.unused_size, v.unused_count > 0)}
      ${btn("build", "Build cache", b.reclaimable, b.size > 0)}</div>`;
  }

  // ---------------------------------------------------------------- tabs
  function imagesTab(d) {
    const imgs = d.images.filter(i => !i.intermediate);
    const f = st.imgFilter;
    const shown = imgs.filter(i => f === "all" || (f === "unused" && !i.in_use) || (f === "old" && i.old_tag) || (f === "dangling" && i.dangling));
    const counts = { all: imgs.length, unused: imgs.filter(i => !i.in_use).length, old: imgs.filter(i => i.old_tag).length, dangling: imgs.filter(i => i.dangling).length };
    const seg = `<div class="seg" data-dk-filter>${[["all", "All"], ["unused", "Unused"], ["old", "Old tags"], ["dangling", "Dangling"]].map(([k, l]) =>
      `<button data-f="${k}" class="${f === k ? "on" : ""}">${l} <span class="dim num">${counts[k]}</span></button>`).join("")}</div>`;
    const inter = d.images.length - imgs.length;
    if (!shown.length) return seg + `<div class="empty">No ${f === "old" ? "old tags" : f === "dangling" ? "dangling images" : f === "unused" ? "unused images" : "images"}.</div>`;
    const rows = sortRows(shown, "images", { size: sizeOf, name: i => i.repository + ":" + i.tag, created: i => i.created });
    const body = rows.map(i => {
      const name = i.repository === "<none>" ? `<span class="dim">&lt;none&gt;</span>` : `${esc(i.repository)}<b>:${esc(i.tag)}</b>`;
      const tags = [i.in_use ? tag("in use", "good", "Used by " + i.used_by.join(", ")) : "", i.dangling ? tag("dangling", "warn", "Untagged layer left over from a rebuild or pull") : "",
        i.old_tag ? tag("old tag", "warn", "A newer tag of this repository exists and no container uses this one") : "",
        !i.in_use && !i.dangling && !i.old_tag ? tag("unused") : "", i.tags_for_id > 1 ? tag(`${i.tags_for_id} tags`, "", "Same image ID under several tags: removing one tag frees nothing until the last one goes") : ""].join("");
      const why = i.in_use ? `Used by ${i.used_by.join(", ") || "a container"} — remove the container first` : "";
      const shared = i.unique_size != null && i.size > i.unique_size ? `<div class="note num d-only">${hb(i.size)} with shared layers</div>` : "";
      return `<tr><td class="dk-n"><div class="dk-name">${name} <span class="dim d-only" style="display:inline">${esc(i.short_id)}</span></div><div class="dk-tags">${tags}</div></td>
        <td class="r num">${hb(sizeOf(i))}${shared}</td><td class="d-only note" style="white-space:nowrap">${esc(i.created_since || (i.created ? ago(i.created) : ""))}</td>
        <td><div class="actions"><span title="${esc(why)}"><button class="small danger" data-dk-rm-image="${esc(i.ref)}" ${why ? "disabled" : ""}>Remove</button></span></div></td></tr>`; }).join("");
    return seg + DH.tbl(`<thead><tr>${th("images", "name", "Image")}${th("images", "size", "Size", "r")}${th("images", "created", "Created", "d-only")}<th></th></tr></thead><tbody>${body}</tbody>`, "fit")
      + `<p class="note">Sizes are what removing the image would free (layers shared with other images aren't counted).${inter ? ` ${plural(inter, "intermediate build layer")} hidden.` : ""}</p>`;
  }

  function groupBy(rows, keyFn) {
    const g = new Map(); rows.forEach(r => { const k = keyFn(r); g.has(k) || g.set(k, []); g.get(k).push(r); });
    return [...g.entries()].sort((a, b) => (a[0] === "") - (b[0] === "") || b[1].reduce((s, r) => s + (r.size || 0), 0) - a[1].reduce((s, r) => s + (r.size || 0), 0));
  }

  function containersTab(d) {
    if (!d.containers.length) return `<div class="empty">No containers.</div>`;
    const sorted = sortRows(d.containers, "containers", { size: c => c.size || 0, name: c => c.name, created: c => c.created });
    const body = groupBy(sorted, c => c.project).map(([proj, rows]) =>
      `<tr class="group-h"><td colspan="4">${proj ? "Compose project · " + esc(proj) : "Not in a Compose project"} <span class="dim">· ${plural(rows.length, "container")}</span></td></tr>` + rows.map(c => {
        const running = ["running", "restarting", "paused"].includes(c.state);
        const why = running ? `${c.name} is ${c.state} — stop it first (docker stop ${c.name})` : "";
        return `<tr><td class="dk-n"><div class="dk-name"><b>${esc(c.name)}</b></div><div class="note dk-name">${esc(c.image)}</div><div class="note m-only">${esc(c.status)}</div>
          <div class="dk-tags">${running ? tag(c.state, "run") : tag(c.state === "exited" ? "stopped" : c.state, "warn")}${c.project ? tag(c.project, "", "Compose project") : ""}</div></td>
          <td class="r num">${c.size == null ? `<span class="dim">—</span>` : hb(c.size)}</td>
          <td class="note d-only">${esc(c.status)}</td>
          <td><div class="actions"><span title="${esc(why)}"><button class="small danger" data-dk-rm-container="${esc(c.id)}" data-name="${esc(c.name)}" ${why ? "disabled" : ""}>Remove</button></span></div></td></tr>`; }).join("")).join("");
    return DH.tbl(`<thead><tr>${th("containers", "name", "Container")}${th("containers", "size", "Size", "r")}<th class="d-only">Status</th><th></th></tr></thead><tbody>${body}</tbody>`, "fit")
      + `<p class="note">Size is the container's own writable layer${d.container_sizes ? "" : " (not measured: <code>docker ps --size</code> timed out)"}. Removing a container keeps its image and named volumes.</p>`;
  }

  function volumesTab(d) {
    if (!d.volumes.length) return `<div class="empty">No volumes.</div>`;
    const sorted = sortRows(d.volumes, "volumes", { size: v => v.size || 0, name: v => v.name });
    const body = groupBy(sorted, v => v.project).map(([proj, rows]) =>
      `<tr class="group-h"><td colspan="4">${proj ? "Compose project · " + esc(proj) : "Not in a Compose project"} <span class="dim">· ${plural(rows.length, "volume")}${rows.some(r => r.size != null) ? " · " + hb(rows.reduce((s, r) => s + (r.size || 0), 0)) : ""}</span></td></tr>` + rows.map(v => {
        const why = v.unused ? "" : `Used by ${v.used_by.join(", ") || v.links + " container(s)"} — remove the container first`;
        return `<tr><td class="dk-n"><div class="dk-name" title="${esc(v.name)}"><b>${esc(shortVol(v.name))}</b></div>
          <div class="dk-tags">${v.unused ? tag("unused", "warn") : tag("in use", "good", "Used by " + (v.used_by.join(", ") || v.links + " container(s)"))}${v.anonymous ? tag("anonymous", "", "Created without a name (e.g. a VOLUME in a Dockerfile)") : ""}${v.project ? tag(v.project, "", "Compose project") : ""}</div></td>
          <td class="r num">${v.size == null ? `<span class="dim">—</span>` : hb(v.size)}</td>
          <td class="note d-only" style="white-space:nowrap">${v.used_by.length ? esc(v.used_by.join(", ")) : v.unused ? "no container" : plural(v.links, "container")}</td>
          <td><div class="actions"><span title="${esc(why)}"><button class="small danger" data-dk-rm-volume="${esc(v.name)}" ${why ? "disabled" : ""}>Remove</button></span></div></td></tr>`; }).join("")).join("");
    return DH.tbl(`<thead><tr>${th("volumes", "name", "Volume")}${th("volumes", "size", "Size", "r")}<th class="d-only">Used by</th><th></th></tr></thead><tbody>${body}</tbody>`, "fit")
      + `<p class="note">Volumes hold data (databases, Jahia/jCustomer/Elasticsearch data): removing one deletes it for good.</p>`;
  }

  function buildTab(d) {
    if (!d.build_cache.length) return `<div class="empty">${d.totals.build_cache.size ? `Build cache: ${hb(d.totals.build_cache.size)} (details need a newer Docker).` : "No build cache."}</div>`;
    const rows = sortRows(d.build_cache, "build", { size: r => r.size, name: r => r.description || r.type });
    const body = rows.map(r => `<tr><td class="dk-n"><div class="dk-name">${esc(r.description || r.id)}</div>
      <div class="dk-tags">${tag(r.type || "cache")}${r.in_use ? tag("in use", "good") : ""}${r.shared ? tag("shared", "", "Shared with other cache records") : ""}</div></td>
      <td class="r num">${hb(r.size)}</td><td class="note" style="white-space:nowrap">${esc(r.last_used || "—")}</td></tr>`).join("");
    return DH.tbl(`<thead><tr>${th("build", "name", "Cache record")}${th("build", "size", "Size", "r")}<th>Last used</th></tr></thead><tbody>${body}</tbody>`, "fit")
      + (d.build_cache_rows > d.build_cache.length ? `<p class="note">Biggest ${d.build_cache.length} of ${d.build_cache_rows} records.</p>` : "")
      + `<p class="note">The build cache is rebuilt as needed: removing it only makes the next <code>docker build</code> slower.</p>`;
  }

  function tabsHtml(d) {
    const t = d.totals; const tabs = [["images", `Images <span class="dim num">${t.images.count}</span>`], ["containers", `Containers <span class="dim num">${t.containers.count}</span>`],
      ["volumes", `Volumes <span class="dim num">${t.volumes.count}</span>`], ["build", `<span class="d-only">Build cache</span><span class="m-only">Cache</span>`]];
    return `<div class="dk-tabs"><div class="seg" role="tablist" data-dk-tabs>${tabs.map(([k, l]) => `<button role="tab" aria-selected="${st.tab === k}" data-tab="${k}" class="${st.tab === k ? "on" : ""}">${l}</button>`).join("")}</div></div>`;
  }

  function renderTab() {
    const box = el.body.querySelector("#dkTab"); if (!box || !last) return;
    box.innerHTML = ({ images: imagesTab, containers: containersTab, volumes: volumesTab, build: buildTab }[st.tab] || imagesTab)(last);
    el.body.querySelectorAll("[data-dk-tabs] button").forEach(b => { const on = b.dataset.tab === st.tab; b.classList.toggle("on", on); b.setAttribute("aria-selected", on); });
  }

  function vmLine(d) {
    const vm = d.vm_disk; if (!vm) return "";
    return `<p class="note">${esc(vm.engine)} disk image <span class="path">${esc(tilde(vm.path))}</span>: <b>${hb(vm.allocated)}</b> on disk (grows up to ${hb(vm.apparent)}).</p>`;
  }

  // ---------------------------------------------------------------- actions
  async function onBulk(kind) {
    const d = last, t = d.totals; const ul = items => `<ul>${items.slice(0, 12).map(x => `<li>${esc(x)}</li>`).join("")}${items.length > 12 ? `<li>… and ${items.length - 12} more</li>` : ""}</ul>`;
    const imgs = d.images.filter(i => !i.intermediate);
    if (kind === "dangling") {
      const l = imgs.filter(i => i.dangling);
      if (await confirmBox("Remove dangling images?", `<p>Runs <code>docker image prune -f</code>: removes ${plural(l.length, "untagged image")} (leftovers of rebuilt or re-pulled tags) — about <b>${hb(t.images.dangling_size)}</b>.</p>${ul(l.map(i => i.short_id + "  " + hb(sizeOf(i))))}<p class="note">Tagged images and anything used by a container are kept.</p>`, "Remove", true)) run("docker-prune-dangling");
    } else if (kind === "unused-images") {
      const l = imgs.filter(i => !i.in_use);
      if (await confirmBox("Remove all unused images?", `<p>Runs <code>docker image prune -a -f</code>: removes <b>every image not used by a container</b> (running or stopped) — ${plural(l.length, "image")}, about <b>${hb(t.images.reclaimable)}</b>.</p>${ul(l.map(i => (i.repository === "<none>" ? i.short_id : i.repository + ":" + i.tag) + "  " + hb(sizeOf(i))))}<p class="note">This includes current tags you may want later (they'll be pulled again on the next <code>docker compose up</code>). To remove only old tags, use the Old tags filter and remove them one by one.</p>`, "Remove images", true)) run("docker-prune-unused-images");
    } else if (kind === "stopped") {
      const l = d.containers.filter(c => !["running", "restarting", "paused"].includes(c.state));
      if (await confirmBox("Remove stopped containers?", `<p>Runs <code>docker container prune -f</code>: removes ${plural(l.length, "stopped container")} — about <b>${hb(t.containers.reclaimable)}</b>.</p>${ul(l.map(c => c.name + (c.project ? "  (" + c.project + ")" : "") + "  " + c.status))}<p class="note">Their images and named volumes are kept; a <code>docker compose up</code> recreates them. Anything written inside a container (outside volumes) is lost.</p>`, "Remove", true)) run("docker-prune-stopped");
    } else if (kind === "volumes") {
      const un = d.volumes.filter(v => v.unused), anon = un.filter(v => v.anonymous), named = un.filter(v => !v.anonymous);
      const sz = l => l.some(v => v.size != null) ? " — " + hb(l.reduce((s, v) => s + (v.size || 0), 0)) : "";
      const ok = await confirmBox("Remove unused volumes?", `<p>Runs <code>docker volume prune -f</code>. Recent Docker versions only remove <b>anonymous</b> volumes this way: ${plural(anon.length, "volume")}${sz(anon)}.</p>
        ${named.length ? `<label class="cf-opt"><input type="checkbox" id="dkVolAll"><span>Also remove <b>${plural(named.length, "unused named volume")}</b>${sz(named)} (<code>--all</code>) — e.g. databases of Compose projects you no longer run:${ul(named.map(v => v.name + (v.size != null ? "  " + hb(v.size) : "")))}</span></label>` : ""}
        <p class="note">Volume data is deleted for good. Volumes used by any container (even stopped) are kept.</p>`, "Remove volumes", true);
      if (ok) { const all = document.getElementById("dkVolAll"); run(all && all.checked ? "docker-prune-volumes-all" : "docker-prune-volumes"); }
    } else if (kind === "build") {
      if (await confirmBox("Remove build cache?", `<p>Runs <code>docker builder prune -f</code>: removes the unused build cache — about <b>${hb(t.build_cache.reclaimable)}</b> of ${hb(t.build_cache.size)}.</p><p class="note">The next <code>docker build</code> re-downloads base layers and re-runs every step.</p>`, "Remove", true)) run("docker-prune-build-cache");
    }
  }

  async function onRemove(b) {
    const d = last;
    if (b.dataset.dkRmImage) {
      const ref = b.dataset.dkRmImage; const i = d.images.find(x => x.ref === ref) || {};
      const freed = i.tags_for_id > 1 && ref.includes(":") ? `<p class="note">This image ID has ${i.tags_for_id} tags: this only removes the tag; space is freed when the last tag goes.</p>` : `<p>Frees about <b>${hb(sizeOf(i))}</b>.</p>`;
      if (await confirmBox("Remove this image?", `<p><code>docker image rm ${esc(ref)}</code></p>${freed}<p class="note">It can be pulled again later.</p>`, "Remove", true)) run("docker-rm-image", ref);
    } else if (b.dataset.dkRmContainer) {
      const c = d.containers.find(x => x.id === b.dataset.dkRmContainer) || {};
      if (await confirmBox("Remove this container?", `<p><code>docker rm ${esc(c.name || "")}</code> — ${esc(c.status || "")}${c.size != null ? `, ${hb(c.size)}` : ""}.</p><p class="note">Its image and named volumes are kept.</p>`, "Remove", true)) run("docker-rm-container", c.id);
    } else if (b.dataset.dkRmVolume) {
      const v = d.volumes.find(x => x.name === b.dataset.dkRmVolume) || {};
      if (await confirmBox("Delete this volume?", `<p><code>docker volume rm ${esc(v.name)}</code>${v.size != null ? ` — <b>${hb(v.size)}</b>` : ""}${v.project ? ` (Compose project ${esc(v.project)})` : ""}.</p><p class="note">The data it holds is deleted for good.</p>`, "Delete volume", true)) run("docker-rm-volume", v.name);
    }
  }

  // ---------------------------------------------------------------- panel
  DH.registerPanel({
    id: "docker", title: "Docker", nav: "Docker", before: "brew",
    init(e) {
      el = e;
      e.tools.innerHTML = `<button class="small" data-dk-refresh>Refresh</button>`;
      e.tools.querySelector("[data-dk-refresh]").onclick = () => run("docker-scan");
      e.body.addEventListener("click", ev => {
        const t = ev.target.closest("button, th.sort"); if (!t || !e.body.contains(t)) return;
        if (t.dataset.dkBulk) return onBulk(t.dataset.dkBulk);
        if (t.dataset.dkRmImage || t.dataset.dkRmContainer || t.dataset.dkRmVolume) return onRemove(t);
        if (t.dataset.tab) { st.tab = t.dataset.tab; return renderTab(); }
        if (t.dataset.f && t.closest("[data-dk-filter]")) { st.imgFilter = t.dataset.f; return renderTab(); }
        if (t.dataset.sort) { const s = st.sort[st.tab]; st.sort[st.tab] = s[0] === t.dataset.sort ? [s[0], -s[1]] : [t.dataset.sort, t.dataset.sort === "name" ? 1 : -1]; renderTab(); }
      });
      e.body.addEventListener("keydown", ev => { if ((ev.key === "Enter" || ev.key === " ") && ev.target.matches("th.sort")) { ev.preventDefault(); ev.target.click(); } });
    },
    render(d, D, e) {
      el = e;
      if (d.available === undefined) {
        e.sub.textContent = ""; e.body.innerHTML = `<div class="empty">Not scanned yet. Click <b>Refresh</b>.</div>`; return; }
      if (!d.available) {
        last = null; e.sub.textContent = d.scanned ? "checked " + ago(d.scanned) : "";
        const msg = d.cli === false ? "The <code>docker</code> command isn't installed — install Docker Desktop, OrbStack or Colima."
          : `${esc(d.reason || "Docker isn't running")} — start ${esc(d.engine || "Docker Desktop")} and click <b>Refresh</b>.`;
        e.body.innerHTML = `<div class="empty">${msg}${d.detail ? `<div class="note" style="margin-top:8px;font-family:var(--mono);font-size:12px">${esc(d.detail)}</div>` : ""}</div>` + (d.vm_disk ? `<div class="dk-grid"><div class="mini"><div class="label">VM disk · ${esc(d.vm_disk.engine)}</div><div class="value num">${hb(d.vm_disk.allocated)}</div><div class="sub2">on disk · ${hb(d.vm_disk.apparent)} max</div></div></div>` + vmLine(d) : "");
        return; }
      last = d;
      e.sub.textContent = `${d.engine}${d.server_version ? " " + d.server_version : ""} · ${hb(d.used)} used · ${hb(d.reclaimable)} reclaimable · scanned ${ago(d.scanned || d._ts)}`;
      e.body.innerHTML = minis(d) + hint(d) + bulk(d) + tabsHtml(d) + `<div id="dkTab"></div>` + vmLine(d);
      renderTab();
    },
  });
})();
