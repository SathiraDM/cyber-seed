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
      rating:     result.rating    || '',
      is_trailer: result.isTrailer || false,
      source_url: item.url,
    }),
  });

  console.log('[cyberseed] Resolved:', item.url, '→', result.quality, result.isTrailer ? '(TRAILER)' : '(FULL)');
}

// This function runs IN the tab context (injected via scripting API)
function extractVideoData() {
  try {
    // ── Page metadata ─────────────────────────────────────────────────
    const title = (
      document.querySelector('.video-page__title, h1.video__title, h1.title, h1')
        ?.textContent?.trim() ||
      document.title.replace(/\s*[-|]\s*FapHouse.*$/i, '').trim()
    );

    const modelEls = document.querySelectorAll(
      '.model-list a, .performers-list a, .video-models a, ' +
      '.video-info__performer a, [data-el-performers] a, ' +
      '.models a, .pornstars a, .actresses a, .performer a'
    );
    const models = [...new Set([...modelEls].map(e => e.textContent.trim()).filter(Boolean))];

    const studio = (
      document.querySelector(
        '.production a, .studio a, .channel a, ' +
        '.video-info__studio a, [data-el-studio], .label a, .network a'
      )?.textContent?.trim() || ''
    );

    const tagEls = document.querySelectorAll(
      '.video-tags a, .tags a, .tag-list a, [data-el-tags] a, .tags-container a'
    );
    const tags = [...new Set([...tagEls].map(e => e.textContent.trim()).filter(Boolean))];

    const duration = document.querySelector(
      '.video-duration, .duration, time[datetime], .meta-duration'
    )?.textContent?.trim() || '';

    const views = document.querySelector(
      '.video-views, .views-count, .meta-views, .view-count'
    )?.textContent?.trim() || '';

    const rating = document.querySelector(
      '.video-rating, .rating-value, .meta-rating, .like-count, .score'
    )?.textContent?.trim() || '';

    const meta = { title, models, studio, tags, duration, views, rating };

    // ── Video URL — attempt 1: data-el-formats ────────────────────────
    const el = document.querySelector('#video-full, .video-player [data-el-formats], [data-el-formats]');
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
            ...meta,
          };
        }
      }
    }

    // ── Video URL — attempt 2: live <video> element src ───────────────
    const videoTag = document.querySelector('video');
    if (videoTag) {
      const src = videoTag.currentSrc || videoTag.src || videoTag.querySelector('source')?.src;
      if (src && src.startsWith('http') && !src.includes('/trailer/')) {
        return { cdnUrl: src, quality: 'video-src', isTrailer: false, ...meta };
      }
      if (src && src.includes('.m3u8')) {
        return { cdnUrl: src, quality: 'hls', isTrailer: false, ...meta };
      }
    }

    return {
      error: 'No video URL found — ensure you are logged in and on a premium video page',
      debug: [...document.querySelectorAll('[data-el-formats]')].map(e => `${e.id || e.className}`.trim()).join('|').slice(0, 300),
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
