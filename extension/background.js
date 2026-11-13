// Disk Health — MV3 service worker.
// Polls the local disk-health service, keeps the toolbar badge current and
// raises notifications for new alerts. Only ever issues GET requests.

const DEFAULT_PORT = 7777;
const ALARM_NAME = "disk-health-poll";
const POLL_MINUTES = 15;
const SEEN_CAP = 500;
const BATCH_THRESHOLD = 3;
const FETCH_TIMEOUT_MS = 8000;

const LEVEL_COLORS = {
  ok: "#2a78d6",
  notice: "#2a78d6",
  warning: "#c98500",
  critical: "#d03b3b",
};
const OFFLINE_COLOR = "#85837d";
const SECTIONS = ["overview", "growth", "caches", "apps", "brew"];

// ---------- settings / urls ----------

async function getPort() {
  try {
    const { port } = await chrome.storage.sync.get({ port: DEFAULT_PORT });
    const n = parseInt(port, 10);
    return n >= 1 && n <= 65535 ? n : DEFAULT_PORT;
  } catch (_) {
    return DEFAULT_PORT;
  }
}

function baseUrl(port) {
  return `http://127.0.0.1:${port}/`;
}

function safeSection(section) {
  return SECTIONS.includes(section) ? section : "overview";
}

// ---------- fetching ----------

async function fetchStatus() {
  const port = await getPort();
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), FETCH_TIMEOUT_MS);
  try {
    const res = await fetch(`${baseUrl(port)}api/status`, {
      method: "GET",
      cache: "no-store",
      signal: ctrl.signal,
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (typeof data !== "object" || data === null || typeof data.free !== "number") {
      throw new Error("Unexpected response");
    }
    return { ok: true, status: data, port };
  } catch (err) {
    return { ok: false, error: String((err && err.message) || err), port };
  } finally {
    clearTimeout(timer);
  }
}

// ---------- badge ----------

function badgeText(freeBytes) {
  const gb = freeBytes / 1e9;
  if (gb >= 1000) return `${(gb / 1000).toFixed(1)}T`;
  return String(Math.round(gb));
}

async function setBadge(result) {
  if (!result.ok) {
    await chrome.action.setBadgeText({ text: "off" });
    await chrome.action.setBadgeBackgroundColor({ color: OFFLINE_COLOR });
    await chrome.action.setTitle({ title: "Disk Health service not running" });
    return;
  }
  const s = result.status;
  const color = LEVEL_COLORS[s.level] || LEVEL_COLORS.ok;
  await chrome.action.setBadgeText({ text: badgeText(s.free) });
  await chrome.action.setBadgeBackgroundColor({ color });
  if (chrome.action.setBadgeTextColor) {
    await chrome.action.setBadgeTextColor({ color: "#ffffff" });
  }
  const pct = typeof s.free_pct === "number" ? ` (${s.free_pct.toFixed(1)}%)` : "";
  const n = Array.isArray(s.alerts) ? s.alerts.length : 0;
  const alertsText = n ? ` — ${n} alert${n === 1 ? "" : "s"}` : "";
  await chrome.action.setTitle({
    title: `Disk Health: ${s.free_h || badgeText(s.free) + " GB"} free${pct}${alertsText}`,
  });
}

// ---------- notifications ----------

// Notification ids carry the target section so clicks work even after the
// service worker has been restarted: "dh|<section>|<random>".
function notificationId(section) {
  return `dh|${safeSection(section)}|${Date.now().toString(36)}${Math.random()
    .toString(36)
    .slice(2, 8)}`;
}

async function notifyNewAlerts(status) {
  const alerts = Array.isArray(status.alerts) ? status.alerts : [];
  const candidates = alerts.filter((a) => a && a.notify && typeof a.id === "string");
  if (!candidates.length) return;

  const { seenAlertIds = [] } = await chrome.storage.local.get("seenAlertIds");
  const seen = new Set(seenAlertIds);
  const fresh = candidates.filter((a) => !seen.has(a.id));
  if (!fresh.length) return;

  // Record as seen before notifying so a crash can't cause repeats.
  const updated = seenAlertIds.concat(fresh.map((a) => a.id)).slice(-SEEN_CAP);
  await chrome.storage.local.set({ seenAlertIds: updated });

  if (fresh.length > BATCH_THRESHOLD) {
    const anyCritical = fresh.some((a) => a.level === "critical");
    await createNotification(notificationId("overview"), {
      message: `${fresh.length} new disk alerts`,
      priority: anyCritical ? 2 : 0,
    });
    return;
  }
  for (const a of fresh) {
    await createNotification(notificationId(a.section), {
      message: String(a.title || "Disk alert"),
      priority: a.level === "critical" ? 2 : 0,
    });
  }
}

function createNotification(id, { message, priority }) {
  return new Promise((resolve) => {
    try {
      chrome.notifications.create(
        id,
        {
          type: "basic",
          iconUrl: "icons/128.png",
          title: "Disk Health",
          message,
          priority,
        },
        () => {
          void chrome.runtime.lastError; // swallow (e.g. notifications disabled)
          resolve();
        }
      );
    } catch (_) {
      resolve();
    }
  });
}

// ---------- dashboard tab ----------

async function openDashboard(section) {
  const port = await getPort();
  const target = `${baseUrl(port)}#${safeSection(section)}`;
  let tabs = [];
  try {
    tabs = await chrome.tabs.query({
      url: [`http://127.0.0.1:${port}/*`, `http://localhost:${port}/*`],
    });
  } catch (_) {
    tabs = [];
  }
  // Prefer a tab showing the dashboard page itself (not /api/...).
  const dash = tabs.find((t) => {
    try {
      const u = new URL(t.url || t.pendingUrl || "");
      return u.pathname === "/" || u.pathname === "/index.html";
    } catch (_) {
      return false;
    }
  });
  if (dash && dash.id != null) {
    await chrome.tabs.update(dash.id, { url: target, active: true });
    if (dash.windowId != null) {
      try {
        await chrome.windows.update(dash.windowId, { focused: true });
      } catch (_) {}
    }
    return;
  }
  await chrome.tabs.create({ url: target, active: true });
}

chrome.notifications.onClicked.addListener(async (id) => {
  if (!id.startsWith("dh|")) return;
  const section = id.split("|")[1];
  await openDashboard(section);
  chrome.notifications.clear(id);
});

// ---------- main refresh ----------

let inflight = null;

async function refresh() {
  if (inflight) return inflight;
  inflight = (async () => {
    const result = await fetchStatus();
    const checkedAt = Date.now();
    try {
      await setBadge(result);
    } catch (e) {
      console.warn("Disk Health: badge update failed", e);
    }
    const snapshot = {
      ok: result.ok,
      status: result.ok ? result.status : null,
      error: result.ok ? null : result.error,
      port: result.port,
      checkedAt,
    };
    await chrome.storage.local.set({ lastResult: snapshot });
    if (result.ok) {
      try {
        await notifyNewAlerts(result.status);
      } catch (e) {
        console.warn("Disk Health: notification failed", e);
      }
    }
    return snapshot;
  })();
  try {
    return await inflight;
  } finally {
    inflight = null;
  }
}

async function ensureAlarm() {
  const existing = await chrome.alarms.get(ALARM_NAME);
  if (!existing || existing.periodInMinutes !== POLL_MINUTES) {
    await chrome.alarms.create(ALARM_NAME, {
      delayInMinutes: POLL_MINUTES,
      periodInMinutes: POLL_MINUTES,
    });
  }
}

chrome.runtime.onInstalled.addListener(async () => {
  await ensureAlarm();
  await refresh();
});

chrome.runtime.onStartup.addListener(async () => {
  await ensureAlarm();
  await refresh();
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM_NAME) refresh();
});

// Re-poll right away when the user changes the port.
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "sync" && changes.port) refresh();
});

// Messages from the popup.
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || typeof msg !== "object") return false;
  if (msg.type === "refresh") {
    refresh().then(sendResponse, (e) => sendResponse({ ok: false, error: String(e) }));
    return true;
  }
  if (msg.type === "open") {
    openDashboard(msg.section).then(
      () => sendResponse({ ok: true }),
      (e) => sendResponse({ ok: false, error: String(e) })
    );
    return true;
  }
  return false;
});
