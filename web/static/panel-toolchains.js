// Toolchains panel: old JDK / Node / Python / Ruby / Go / Rust versions (diskhealth/features/toolchains.py)
(() => {
  const { esc, hkb, ago, tilde, tbl } = DH;
  const LANGS = ["Java", "Node", "Python", "Ruby", "Go", "Rust"];
  let filter = "all", last = null;

  function method(i) {
    if (i.manager === "System JDK" && i.admin) return "macOS will ask for your administrator password, then the JDK bundle is deleted (not recoverable).";
    if (i.manager === "nvm") return "The folder is deleted, exactly what <code>nvm uninstall</code> does.";
    if (i.manager === "SDKMAN") return "The folder is deleted, exactly what <code>sdk uninstall</code> does.";
    if (i.manager === "pyenv") return "Runs <code>pyenv uninstall -f</code> (or moves the folder to the Trash if pyenv isn't found).";
    if (i.manager === "rustup") return "Runs <code>rustup toolchain uninstall</code> (or moves it to the Trash if rustup isn't found).";
    if (i.manager === "rbenv") return "Runs <code>rbenv uninstall -f</code> (or moves it to the Trash).";
    if (i.manager === "fnm") return "Runs <code>fnm uninstall</code> (or moves it to the Trash).";
    if (i.manager === "uv") return "Runs <code>uv python uninstall</code> (or moves it to the Trash).";
    return "The folder is moved to the Trash: you can put it back from the Trash panel.";
  }

  async function remove(path) {
    const i = (last.items || []).find(x => x.path === path); if (!i) return;
    const ok = await DH.confirmBox(`Remove ${i.lang} ${i.name}?`,
      `<p>${esc(i.manager)} · ${hkb(i.kb)} · last used ${i.last_used ? ago(i.last_used) : "never recorded"}</p>
       <ul><li>${esc(i.path)}</li></ul><p>${method(i)}</p>
       ${i.venvs ? `<p><b>${i.venvs} virtualenv${i.venvs > 1 ? "s" : ""}</b> built on this version will stop working.</p>` : ""}
       <p class="note">Projects that need this exact version will have to reinstall it.</p>`,
      i.manager === "System JDK" && i.admin ? "Remove (admin)" : "Remove", true);
    if (ok) DH.run("toolchain-remove", i.path);
  }

  function row(i, max) {
    const pins = i.pinned_by || [];
    const tags = [
      i.default ? `<span class="tag good" title="Default / current version">default</span>` : "",
      pins.length ? `<span class="tag" title="${esc("Pinned by:\n" + pins.map(tilde).join("\n"))}">pinned ×${pins.length}</span>` : "",
      i.venvs ? `<span class="tag" title="pyenv virtualenvs built on this version">${i.venvs} venv${i.venvs > 1 ? "s" : ""}</span>` : "",
      i.unused ? `<span class="tag warn">unused</span>` : "",
      i.symlink ? `<span class="tag">symlink</span>` : "",
    ].filter(Boolean).join(" ");
    const sub = [i.vendor, i.lang === "Java" && (i.aliases || []).length ? "jenv: " + i.aliases.join(", ") : ""].filter(Boolean).join(" · ");
    const can = i.removable && !i.default && !pins.length;
    return `<tr><td><div><b>${esc(i.version || i.name)}</b>${i.name !== i.version ? ` <span class="note d-only">${esc(i.name)}</span>` : ""}</div>
        ${sub ? `<div class="note">${esc(sub)}</div>` : ""}<div class="note path d-only" title="${esc(i.path)}">${esc(tilde(i.path))}</div>
        <div class="m-only note">${esc(i.manager)} · used ${i.last_used ? ago(i.last_used) : "never recorded"}</div>${tags ? `<div class="m-only" style="margin-top:3px">${tags}</div>` : ""}</td>
      <td class="d-only"><span class="tag">${esc(i.manager)}</span></td>
      <td class="r num">${i.symlink ? `<span class="note">—</span>` : hkb(i.kb)}</td>
      <td class="bar-cell"><div class="bar" style="width:${(100 * i.kb / max).toFixed(1)}%"></div></td>
      <td class="d-only" style="white-space:nowrap">${i.last_used ? ago(i.last_used) : `<span class="note">no record</span>`}</td>
      <td class="d-only">${tags}</td>
      <td><div class="actions"><button class="small danger" data-tc-rm="${esc(i.path)}" ${can ? "" : "disabled"} title="${esc(can ? "Remove this version" : i.reason || "Can't be removed here")}">Remove</button></div></td></tr>`;
  }

  DH.registerPanel({
    id: "toolchains", title: "Old toolchain versions", nav: "Toolchains", before: "git",
    init(el) {
      el.tools.innerHTML = `<div class="seg"><button data-f="unused">Unused</button><button data-f="all" class="on">All</button></div>
        <button class="small" data-tc-refresh>Refresh</button>`;
      el.tools.addEventListener("click", e => {
        const f = e.target.closest("[data-f]");
        if (f) { filter = f.dataset.f; el.tools.querySelectorAll("[data-f]").forEach(b => b.classList.toggle("on", b === f)); if (last) this.render(last, DH.D, el); }
        if (e.target.closest("[data-tc-refresh]")) DH.run("toolchains-scan");
      });
      el.body.addEventListener("click", e => { const b = e.target.closest("[data-tc-rm]"); if (b) remove(b.dataset.tcRm); });
    },
    render(data, D, el) {
      last = data;
      if (!data.items) { el.sub.textContent = ""; el.body.innerHTML = `<div class="empty">Not scanned yet. Click <b>Refresh</b> (or wait for the daily scan).</div>`; return; }
      const items = data.items;
      el.sub.textContent = `${data.count} versions · ${hkb(data.total_kb)} · checked ${ago(data.ts)}`;
      const pinned = items.filter(i => (i.pinned_by || []).length).length, defaults = items.filter(i => i.default).length;
      const byLang = LANGS.map(l => [l, items.filter(i => i.lang === l)]).filter(([, v]) => v.length);
      const head = `<div class="brew-grid">
        <div class="mini"><div class="label">Installed versions</div><div class="value num">${hkb(data.total_kb)}</div><div class="note">${data.count} versions · ${byLang.map(([l, v]) => `${v.length} ${l}`).join(", ") || "none found"}</div></div>
        <div class="mini"><div class="label">Unused for ${data.unused_days}+ days</div><div class="value num">${data.unused_count}</div><div class="note">${hkb(data.unused_kb)} reclaimable · not default, not pinned</div></div>
        <div class="mini"><div class="label">Default / current</div><div class="value num">${defaults}</div><div class="note">never removed here</div></div>
        <div class="mini"><div class="label">Pinned by projects</div><div class="value num">${pinned}</div><div class="note" title="${esc((data.roots || []).join("\n"))}">.nvmrc, .python-version, .tool-versions… in ${(data.roots || []).length} project folder${(data.roots || []).length === 1 ? "" : "s"}</div></div></div>`;
      const shown = filter === "unused" ? items.filter(i => i.unused) : items;
      if (!items.length) { el.body.innerHTML = head + `<div class="empty">No version managers found (nvm, pyenv, SDKMAN, rustup, JDKs…).</div>`; return; }
      if (!shown.length) { el.body.innerHTML = head + `<div class="empty">Every version is the default, pinned by a project, or was used in the last ${data.unused_days} days.</div>`; return; }
      const max = Math.max(...items.map(i => i.kb), 1);
      el.body.innerHTML = head + tbl(`<thead><tr><th>Version</th><th class="d-only">Manager</th><th class="r">Size</th><th class="bar-cell"></th><th class="d-only">Last used</th><th class="d-only">State</th><th></th></tr></thead><tbody>${
        LANGS.map(l => { const g = shown.filter(i => i.lang === l); if (!g.length) return "";
          const kb = g.reduce((s, i) => s + i.kb, 0);
          return `<tr class="group-h"><td colspan="7">${esc(l)} <span class="note">· ${g.length} · ${hkb(kb)}</span></td></tr>` + g.map(i => row(i, max)).join(""); }).join("")}</tbody>`, "fit")
        + `<p class="note" style="margin:10px 0 0">“Last used” is the last access time of the version's executables: macOS updates it lazily, so treat it as approximate. Homebrew JDKs are removed from the Homebrew panel.</p>`;
    },
  });
})();
