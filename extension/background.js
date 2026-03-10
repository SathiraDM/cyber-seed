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
      cdn_url:    result.cdnUrl,
      title:      result.title     || '',
      quality:    result.quality   || '',
      models:     result.models    || [],
      studio:     result.studio    || '',
      tags:       result.tags      || [],
      duration:   result.duration  || '',
      views:      result.views     || '',
      published:  result.published || '',
      is_trailer: result.isTrailer || false,
      is_hls:     result.isHls    || false,
      source_url: item.url,
      debug_dump: result.debugDump || {},
    }),
  });

  console.log('[cyberseed] Resolved:', item.url, '→', result.quality, result.isTrailer ? '(TRAILER)' : '(FULL)');
}

// This function runs IN the tab context (injected via scripting API)
async function extractVideoData() {
  // Wait for the SPA to hydrate data-el-formats (up to 10s)
  await new Promise(resolve => {
    const deadline = Date.now() + 10000;
    function poll() {
      if (document.querySelector('[data-el-formats]') || Date.now() >= deadline) {
        resolve();
      } else {
        setTimeout(poll, 300);
      }
    }
    poll();
  });

  try {
    // ── Page metadata — use href patterns, reliable across site updates ──
    const title = (
      document.querySelector('h1')?.textContent?.trim() ||
      document.title.replace(/\s*[-|]\s*FapHouse.*$/i, '').trim()
    );

    // Models = /pornstars/ or /models/ links, deduplicated, near top of page
    const modelLinks = document.querySelectorAll('a[href*="/pornstars/"], a[href*="/models/"]');
    const models = [...new Set([...modelLinks].map(e => e.textContent.trim()).filter(Boolean))];

    // Studio = first /studios/ link on the page (the video's own studio)
    const studioLink = document.querySelector('a[href*="/studios/"]');
    const studio = studioLink?.textContent?.trim() || '';

    // Tags = category links (/c/) + search query links
    const tagLinks = document.querySelectorAll('a[href*="/c/"], a[href*="/search/videos?q="]');
    const tags = [...new Set([...tagLinks].map(e => e.textContent.trim()).filter(Boolean))];

    // Duration — look for time-like text (MM:SS or HH:MM:SS)
    const allText = [...document.querySelectorAll('*')].find(el =>
      el.childNodes.length === 1 &&
      el.childNodes[0].nodeType === 3 &&
      /^\d{1,2}:\d{2}(:\d{2})?$/.test(el.textContent.trim())
    );
    const duration = allText?.textContent?.trim() || '';

    // Views — look for the views count element
    const viewsEl = document.querySelector(
      '.views, .video-views, .view-count, [class*="view"], [class*="Views"]'
    );
    const views = viewsEl?.textContent?.trim().replace(/[^\d,KkMm.]/g, '') || '';

    // Published date
    const dateEl = document.querySelector('time, [class*="date"], [class*="Date"], [class*="publish"]');
    const published = dateEl?.getAttribute('datetime') || dateEl?.textContent?.trim() || '';

    const meta = { title, models, studio, tags, duration, views, published };

    // ── Video URL ──────────────────────────────────────────────────────
    // Collect debug info
    const debugDump = {};
    document.querySelectorAll('[data-el-formats],[data-el-hls-url],[data-el-src],[data-el-url],[data-el-stream],[data-src],[data-url],[data-hls]').forEach(el => {
      for (const attr of el.attributes) {
        if (attr.name.startsWith('data-')) {
          debugDump[attr.name] = attr.value.slice(0, 300);
        }
      }
    });
    const videoEl = document.querySelector('video');
    if (videoEl) {
      debugDump['video.currentSrc'] = videoEl.currentSrc?.slice(0, 300) || '';
      debugDump['video.src'] = videoEl.src?.slice(0, 300) || '';
      videoEl.querySelectorAll('source').forEach((s, i) => { debugDump[`source[${i}].src`] = s.src?.slice(0, 300) || ''; });
    }

    // ── 1. Prefer HLS stream (full video) over data-el-formats (trailer only on faphouse) ──
    const hlsEl = document.querySelector('[data-el-hls-url]');
    const hlsUrl = hlsEl?.getAttribute('data-el-hls-url');
    if (hlsUrl) {
      return { cdnUrl: hlsUrl, quality: '1080', isHls: true, isTrailer: false, debugDump, ...meta };
    }

    // ── 2. Fall back to data-el-formats mp4 links ──
    const el = document.querySelector('[data-el-formats]');
    if (el) {
      const formatsRaw = el.getAttribute('data-el-formats');
      if (formatsRaw) {
        const parsed = JSON.parse(formatsRaw);
        let formatsArr;
        if (Array.isArray(parsed)) {
          formatsArr = parsed;
        } else {
          formatsArr = Object.entries(parsed).map(([label, val]) => ({
            label,
            url: typeof val === 'string' ? val :
              (val.url || val.src || val.file ||
               Object.values(val).find(v => typeof v === 'string' && v.startsWith('http'))),
          }));
        }

        // Prefer full (non-trailer) URLs; only fall back to trailer if nothing else
        const fullFormats = formatsArr.filter(f => f.url && !f.url.includes('/trailer/'));
        const pool = fullFormats.length ? fullFormats : formatsArr;

        const priorities = ['2160', '4k', '1080', '720', '480', '360'];
        let best = null;
        for (const p of priorities) {
          best = pool.find(f => f.label && f.label.toLowerCase().includes(p));
          if (best) break;
        }
        if (!best && pool.length) best = pool[0];

        if (best?.url) {
          return {
            cdnUrl:    best.url,
            quality:   best.label || 'unknown',
            isTrailer: fullFormats.length === 0,
            debugDump,
            ...meta,
          };
        }
      }
    }

    // Fallback: live <video> src
    const videoTag2 = document.querySelector('video');
    if (videoTag2) {
      const src = videoTag2.currentSrc || videoTag2.src ||
                  videoTag2.querySelector('source')?.src;
      if (src?.startsWith('http')) {
        return { cdnUrl: src, quality: 'auto', isTrailer: src.includes('/trailer/'), debugDump, ...meta };
      }
    }

    return {
      error: 'No video URL found — make sure you are logged in with a premium faphouse account',
      debugDump,
      ...meta,
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
