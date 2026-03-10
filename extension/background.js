// ─────────────────────────────────────────────────────────────────────
// CyberSeed FapHouse Grabber — Background Service Worker
// Polls the CyberSeed API for queued faphouse URLs, opens each in a
// tab, waits for the content script to extract CDN URLs, then sends
// the download URL back to the API.
// ─────────────────────────────────────────────────────────────────────

const POLL_INTERVAL = 5000;   // ms between queue checks
const PAGE_TIMEOUT  = 20000;  // ms to wait for content script response

// ── Config (set via options page; these defaults are used as fallback) ─
const DEFAULT_API_BASE = 'http://35.232.101.41:8888';
const DEFAULT_API_KEY  = '';   // no hardcoded key — must be set in Options

let API_BASE = DEFAULT_API_BASE;
let API_KEY  = '';

async function loadConfig() {
  const data = await chrome.storage.local.get(['apiBase', 'apiKey']);
  API_BASE = data.apiBase || DEFAULT_API_BASE;
  API_KEY  = data.apiKey  || '';

  // If API key not yet configured, try loading from credentials.json mounted
  // at runtime via docker-compose (NOT committed to git — gitignored file).
  if (!API_KEY) {
    try {
      const resp = await fetch(chrome.runtime.getURL('credentials.json'));
      if (resp.ok) {
        const creds = await resp.json();
        API_BASE = creds.apiBase || API_BASE;
        API_KEY  = creds.apiKey  || '';
        // Persist into storage so it survives extension reloads
        if (API_KEY) {
          await chrome.storage.local.set({ apiBase: API_BASE, apiKey: API_KEY });
          console.log('[cyberseed] Loaded credentials from credentials.json');
        }
      }
    } catch (_) {
      // credentials.json not present — configure via Options page instead
    }
  }
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

  // Ask content script to extract video data
  // (the function itself waits up to 10s inside the tab for the SPA to hydrate)
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

  // Send resolved CDN URL + metadata back to API
  await apiFetch('/api/faphouse/resolve', {
    method: 'POST',
    body: JSON.stringify({
      id:         item.id,
      cdn_url:   result.cdnUrl,
      title:     result.title     || '',
      quality:   result.quality   || '',
      models:    result.models    || [],
      studio:    result.studio    || '',
      tags:      result.tags      || [],
      duration:  result.duration  || '',
      views:     result.views     || '',
      published: result.published || '',
      is_hls:    result.isHls    || false,
      source_url: item.url,
    }),
  });

  console.log('[cyberseed] Resolved:', item.url, '→', result.quality, '(HLS FULL)');
}

// This function runs IN the tab context (injected via scripting API)
async function extractVideoData() {
  // Wait for the SPA to hydrate data-el-hls-url (up to 10s)
  await new Promise(resolve => {
    const deadline = Date.now() + 10000;
    function poll() {
      if (document.querySelector('[data-el-hls-url]') || Date.now() >= deadline) {
        resolve();
      } else {
        setTimeout(poll, 300);
      }
    }
    poll();
  });

  try {
    // ── Page metadata — read from data attributes (video-specific, not page-wide) ──
    const playerEl = document.querySelector('[data-el-hls-url]');

    const title = (
      playerEl?.getAttribute('data-el-video-title') ||
      document.querySelector('h1')?.textContent?.trim() ||
      document.title.replace(/\s*[-|]\s*FapHouse.*$/i, '').trim()
    );

    // data-el-pornstar-names is a JSON array of only this video's performers
    let models = [];
    const studio = playerEl?.getAttribute('data-el-studio-name') || '';
    try {
      const raw = playerEl?.getAttribute('data-el-pornstar-names');
      if (raw) {
        const studioLower = studio.toLowerCase();
        // FapHouse sometimes includes the studio name as the first entry in the
        // performers array — filter it out so it only appears in the studio field.
        models = JSON.parse(raw)
          .filter(Boolean)
          .filter(name => name.toLowerCase() !== studioLower);
      }
    } catch (_) {}

    const tagLinks = document.querySelectorAll('a[href*="/c/"], a[href*="/search/videos?q="]');
    const tags = [...new Set([...tagLinks].map(e => e.textContent.trim()).filter(Boolean))];

    // data-el-duration is in seconds
    let duration = '';
    const durSecs = parseInt(playerEl?.getAttribute('data-el-duration') || '', 10);
    if (!isNaN(durSecs) && durSecs > 0) {
      const h = Math.floor(durSecs / 3600);
      const m = Math.floor((durSecs % 3600) / 60);
      const s = durSecs % 60;
      duration = h > 0
        ? `${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`
        : `${m}:${String(s).padStart(2,'0')}`;
    }

    const viewsEl = document.querySelector('.views, .video-views, .view-count, [class*="view"], [class*="Views"]');
    const views = viewsEl?.textContent?.trim().replace(/[^\d,KkMm.]/g, '') || '';

    const dateEl = document.querySelector('time[datetime]');
    const published = dateEl?.getAttribute('datetime') ||
      document.querySelector('[class*="date"], [class*="publish"]')?.textContent
        ?.replace(/^published[:\s]*/i, '').trim() || '';

    const meta = { title, models, studio, tags, duration, views, published };

    // ── HLS stream URL (full video) ──
    const hlsUrl = document.querySelector('[data-el-hls-url]')?.getAttribute('data-el-hls-url');
    if (hlsUrl) {
      return { cdnUrl: hlsUrl, quality: '1080', isHls: true, ...meta };
    }

    return { error: 'No HLS URL found — make sure you are logged in with a premium faphouse account', ...meta };
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
