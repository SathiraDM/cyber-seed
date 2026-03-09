// ─────────────────────────────────────────────────────────────────────
// CyberSeed FapHouse Grabber — Background Service Worker
// Polls the CyberSeed API for queued faphouse URLs, opens each in a
// tab, waits for the content script to extract CDN URLs, then sends
// the download URL back to the API.
// ─────────────────────────────────────────────────────────────────────

const POLL_INTERVAL = 5000;   // ms between queue checks
const PAGE_TIMEOUT  = 20000;  // ms to wait for content script response

// ── Config (set via storage) ─────────────────────────────────────────
let API_BASE = '';   // e.g. http://35.232.101.41:8888
let API_KEY  = '';   // QBT_WEBUI_PASS

async function loadConfig() {
  const data = await chrome.storage.local.get(['apiBase', 'apiKey']);
  API_BASE = data.apiBase || '';
  API_KEY  = data.apiKey  || '';
}

// ── API helpers ──────────────────────────────────────────────────────
async function apiFetch(path, opts = {}) {
  if (!API_BASE || !API_KEY) return null;
  const url = `${API_BASE}${path}`;
  const headers = { 'X-Api-Key': API_KEY, 'Content-Type': 'application/json', ...(opts.headers || {}) };
  try {
    const resp = await fetch(url, { ...opts, headers });
    if (!resp.ok) return null;
    return resp.json();
  } catch (e) {
    console.error('[cyberseed]', e);
    return null;
  }
}

// ── Processing state ─────────────────────────────────────────────────
let processing = false;

async function pollQueue() {
  if (processing || !API_BASE || !API_KEY) return;
  processing = true;
  try {
    const queue = await apiFetch('/api/faphouse/queue');
    if (!queue || !queue.length) return;

    for (const item of queue) {
      console.log('[cyberseed] Processing:', item.url);
      await processItem(item);
    }
  } catch (e) {
    console.error('[cyberseed] poll error:', e);
  } finally {
    processing = false;
  }
}

async function processItem(item) {
  // Mark as processing
  await apiFetch(`/api/faphouse/queue/${item.id}/status`, {
    method: 'POST',
    body: JSON.stringify({ status: 'processing' }),
  });

  // Open the faphouse video page in a new tab
  let tab;
  try {
    tab = await chrome.tabs.create({ url: item.url, active: false });
  } catch (e) {
    console.error('[cyberseed] tab create error:', e);
    await reportFail(item.id, 'Failed to open tab');
    return;
  }

  // Wait for tab to finish loading
  await waitForTabLoad(tab.id);

  // Small extra delay for JS to populate data attributes
  await sleep(3000);

  // Ask content script to extract video data
  let result;
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: extractVideoData,
    });
    result = results?.[0]?.result;
  } catch (e) {
    console.error('[cyberseed] script inject error:', e);
    await reportFail(item.id, 'Failed to inject content script');
    chrome.tabs.remove(tab.id).catch(() => {});
    return;
  }

  // Close tab
  chrome.tabs.remove(tab.id).catch(() => {});

  if (!result || !result.cdnUrl) {
    await reportFail(item.id, result?.error || 'No CDN URL found — may not be premium or not logged in');
    return;
  }

  // Send resolved CDN URL back to API
  await apiFetch('/api/faphouse/resolve', {
    method: 'POST',
    body: JSON.stringify({
      id:       item.id,
      cdn_url:  result.cdnUrl,
      title:    result.title || '',
      quality:  result.quality || '',
    }),
  });

  console.log('[cyberseed] Resolved:', item.url, '→', result.quality);
}

// This function runs IN the tab context (injected via scripting API)
function extractVideoData() {
  try {
    const el = document.querySelector('#video-full, [data-el-formats]');
    if (!el) {
      return { error: 'No video element found on page' };
    }

    const formatsRaw = el.getAttribute('data-el-formats');
    if (!formatsRaw) {
      return { error: 'No data-el-formats attribute — not premium?' };
    }

    const formats = JSON.parse(formatsRaw);
    // formats is an array of objects like: { "label": "1080p", "url": "https://video-nss.flixcdn.com/..." }
    // Pick highest quality
    const priorities = ['2160', '1080', '720', '480', '360'];
    let best = null;
    for (const p of priorities) {
      best = formats.find(f => f.label && f.label.includes(p));
      if (best) break;
    }
    if (!best && formats.length) best = formats[0];

    if (!best || !best.url) {
      return { error: 'formats parsed but no valid URL found' };
    }

    const title = document.querySelector('h1.video__title')?.textContent?.trim() || document.title;

    return {
      cdnUrl:  best.url,
      quality: best.label || 'unknown',
      title:   title,
    };
  } catch (e) {
    return { error: `Extract error: ${e.message}` };
  }
}

async function reportFail(id, reason) {
  await apiFetch(`/api/faphouse/queue/${id}/status`, {
    method: 'POST',
    body: JSON.stringify({ status: 'failed', error: reason }),
  });
}

function waitForTabLoad(tabId) {
  return new Promise((resolve) => {
    const timeout = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      resolve(); // resolve anyway after timeout
    }, PAGE_TIMEOUT);

    function listener(id, info) {
      if (id === tabId && info.status === 'complete') {
        clearTimeout(timeout);
        chrome.tabs.onUpdated.removeListener(listener);
        resolve();
      }
    }
    chrome.tabs.onUpdated.addListener(listener);
  });
}

function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

// ── Init ─────────────────────────────────────────────────────────────
// MV3 service workers go idle after ~30s; setInterval won't survive.
// Use chrome.alarms (minimum 1 min) to wake the SW periodically, and
// also poll immediately on every SW start.
chrome.alarms.create('poll', { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === 'poll') pollQueue();
});

// Immediate poll every time the service worker starts (on install,
// browser start, or wake from idle).
loadConfig().then(() => {
  pollQueue();
  console.log('[cyberseed] Extension loaded — alarm set, immediate poll fired');
});

// Also re-poll when extension storage settings change (catches config save)
chrome.storage.onChanged.addListener(() => {
  loadConfig().then(() => pollQueue());
});
