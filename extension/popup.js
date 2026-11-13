// Disk Health popup. All network and tab work goes through the service worker.

const DEFAULT_PORT = 7777;
const LEVEL_LABEL = { critical: "Critical", warning: "Warning", notice: "Notice", info: "Info" };
const LEVEL_ORDER = { critical: 0, warning: 1, notice: 2, info: 3 };
const STATE_LABEL = { ok: "OK", notice: "Notice", warning: "Low", critical: "Critical" };

const $ = (id) => document.getElementById(id);

function send(msg) {
  return new Promise((resolve) => {
    try {
      chrome.runtime.sendMessage(msg, (resp) => {
        void chrome.runtime.lastError;
        resolve(resp);
      });
    } catch (_) {
      resolve(null);
    }
  });
}

function formatBytes(bytes) {
  if (typeof bytes !== "number" || !isFinite(bytes)) return "";
  const gb = bytes / 1e9;
  if (gb >= 1000) return `${(gb / 1000).toFixed(1)} TB`;
  if (gb >= 100) return `${Math.round(gb)} GB`;
  return `${gb.toFixed(1)} GB`;
}

function formatTime(ms) {
  if (!ms) return "Not checked yet";
  const d = new Date(ms);
  const sameDay = new Date().toDateString() === d.toDateString();
  const t = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return `Last checked ${sameDay ? t : d.toLocaleDateString() + " " + t}`;
}

function render(snap) {
  $("loading").hidden = true;
  $("checked").textContent = formatTime(snap && snap.checkedAt);

  if (!snap || !snap.ok || !snap.status) {
    $("online").hidden = true;
    $("offline").hidden = false;
    $("state").textContent = "Offline";
    $("state").className = "state";
    $("offline-detail").textContent = snap
      ? `Tried http://127.0.0.1:${snap.port}/ — check the port in Settings if you changed it.`
      : "";
    return;
  }

  const s = snap.status;
  $("offline").hidden = true;
  $("online").hidden = false;

  const level = s.level || "ok";
  $("state").textContent = STATE_LABEL[level] || level;
  $("state").className = `state ${level}`;

  $("free").textContent = s.free_h || formatBytes(s.free);
  const pct = typeof s.free_pct === "number" ? s.free_pct : (s.total ? (s.free / s.total) * 100 : null);
  $("pct").textContent = pct != null ? `${pct.toFixed(1)}% free` : "";

  const used = pct != null ? Math.max(0, Math.min(100, 100 - pct)) : 0;
  const fill = $("meter-fill");
  fill.style.width = `${used}%`;
  fill.className = `meter-fill ${level}`;
  $("meter").setAttribute("aria-valuenow", used.toFixed(1));
  $("meter").setAttribute("aria-valuetext", `${used.toFixed(0)}% used`);
  $("total").textContent = s.total
    ? `${formatBytes(s.total - s.free)} used of ${formatBytes(s.total)}${s.busy ? " · scan in progress" : ""}`
    : s.busy ? "Scan in progress" : "";

  const pr = s.progress || {};
  $("scan").hidden = !pr.active;
  clearTimeout(window.__scanPoll);
  if (pr.active) {
    const mins = (x) => (x == null ? "" : x < 90 ? `${Math.round(x)} s` : `${Math.round(x / 60)} min`);
    const pc = pr.pct == null ? null : Math.round(pr.pct * 100);
    $("scan-title").textContent = (pr.title || "Working") + (pc != null ? ` · ${pc}%` : "");
    $("scan-text").textContent = (pr.steps > 1 ? `${pr.step} (${pr.step_index + 1}/${pr.steps})` : (pr.current || "").slice(-60))
      + (pr.eta != null ? ` · ~${mins(pr.eta)} left` : "");
    $("scan-fill").style.width = pc != null ? `${pc}%` : "100%";
    $("scan-fill").style.opacity = pc != null ? "1" : "0.35";
    // Keep the popup live while a scan runs.
    window.__scanPoll = setTimeout(async () => render(await send({ type: "refresh" })), 2000);
  }

  const list = $("alerts");
  list.textContent = "";
  const alerts = (Array.isArray(s.alerts) ? s.alerts.slice() : []).sort(
    (a, b) => (LEVEL_ORDER[a.level] ?? 9) - (LEVEL_ORDER[b.level] ?? 9)
  );
  $("alerts-title").hidden = alerts.length === 0;
  $("no-alerts").hidden = alerts.length !== 0;
  for (const a of alerts) {
    const li = document.createElement("li");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "alert";
    btn.title = `Open ${a.section || "overview"} in dashboard`;
    const lvl = document.createElement("span");
    lvl.className = `lvl ${a.level || "info"}`;
    lvl.textContent = LEVEL_LABEL[a.level] || "Info";
    const title = document.createElement("span");
    title.className = "alert-title";
    title.textContent = a.title || "";
    btn.append(lvl, title);
    btn.addEventListener("click", () => openSection(a.section));
    li.append(btn);
    list.append(li);
  }
}

async function openSection(section) {
  await send({ type: "open", section: section || "overview" });
  window.close();
}

async function doRefresh() {
  const btn = $("refresh");
  btn.disabled = true;
  btn.textContent = "Checking…";
  const snap = await send({ type: "refresh" });
  render(snap);
  btn.disabled = false;
  btn.textContent = "Refresh";
}

async function loadPort() {
  try {
    const { port } = await chrome.storage.sync.get({ port: DEFAULT_PORT });
    $("port").value = port;
  } catch (_) {
    $("port").value = DEFAULT_PORT;
  }
}

async function savePort(ev) {
  ev.preventDefault();
  const n = parseInt($("port").value, 10);
  if (!(n >= 1 && n <= 65535)) {
    $("port-hint").textContent = "Enter a port between 1 and 65535.";
    return;
  }
  await chrome.storage.sync.set({ port: n });
  $("port-hint").textContent = `Saved — using port ${n}.`;
  doRefresh();
}

async function copyCmd() {
  try {
    await navigator.clipboard.writeText($("cmd").textContent);
    $("copy").textContent = "Copied";
  } catch (_) {
    const r = document.createRange();
    r.selectNodeContents($("cmd"));
    const sel = getSelection();
    sel.removeAllRanges();
    sel.addRange(r);
    $("copy").textContent = "Selected";
  }
  setTimeout(() => ($("copy").textContent = "Copy"), 1500);
}

document.addEventListener("DOMContentLoaded", async () => {
  $("open").addEventListener("click", () => openSection("overview"));
  $("refresh").addEventListener("click", doRefresh);
  $("settings-form").addEventListener("submit", savePort);
  $("copy").addEventListener("click", copyCmd);
  loadPort();

  // Show the cached result instantly, then refresh in the background.
  try {
    const { lastResult } = await chrome.storage.local.get("lastResult");
    if (lastResult) render(lastResult);
  } catch (_) {}
  doRefresh();
});
