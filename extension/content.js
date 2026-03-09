// Content script — runs on faphouse.com/videos/* pages
// Acts as a fallback; main extraction is done by background.js via chrome.scripting.executeScript
// This script just signals to the background that the page is ready.
(function() {
  // Nothing to do here — background.js handles extraction via scripting API.
  // This file exists so the content_scripts manifest entry is valid.
})();
