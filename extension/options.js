document.getElementById('save').addEventListener('click', () => {
  const apiBase = document.getElementById('apiBase').value.trim().replace(/\/+$/, '');
  const apiKey  = document.getElementById('apiKey').value;
  chrome.storage.local.set({ apiBase, apiKey }, () => {
    document.getElementById('status').textContent = '✓ Saved!';
    setTimeout(() => { document.getElementById('status').textContent = ''; }, 2000);
  });
});

// Load on open
chrome.storage.local.get(['apiBase', 'apiKey'], (data) => {
  document.getElementById('apiBase').value = data.apiBase || '';
  document.getElementById('apiKey').value  = data.apiKey  || '';
});
