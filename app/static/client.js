// Shared browser transport and startup recovery. No credentials are stored here.
const tabMemory = new Map();
const browserSession = {
  getItem(key) {
    try {
      return sessionStorage.getItem(key) ?? tabMemory.get(key) ?? null;
    } catch {
      return tabMemory.get(key) ?? null;
    }
  },
  setItem(key, value) {
    tabMemory.set(key, value);
    try {
      sessionStorage.setItem(key, value);
    } catch {}
  }
};

function requestId() {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  // getRandomValues also works on HTTP development hosts without randomUUID.
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  return Array.from(bytes, (n, i) => ([4, 6, 8, 10].includes(i) ? '-' : '') + n.toString(16).padStart(2, '0')).join('');
}

async function httpJSON(path, options = {}) {
  const {
    signal,
    ...rest
  } = options;
  const controller = new AbortController();
  let timedOut = false;
  const relay = () => controller.abort();
  if (signal?.aborted) relay();
  else signal?.addEventListener('abort', relay, {
    once: true
  });
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, path.endsWith('/messages') ? 70000 : 12000);
  try {
    const response = await fetch(path, {
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json'
      },
      ...rest,
      signal: controller.signal
    });
    if (!response.headers.get('content-type')?.includes('application/json')) {
      throw new Error('The address did not return the demo service. Check the port or forwarded URL.');
    }
    const data = await response.json();
    if (!response.ok) {
      const failure = new Error(data.error || data.detail || 'Service unavailable');
      failure.status = response.status;
      throw failure;
    }
    return data;
  } catch (failure) {
    if (timedOut) throw new Error('Connection timed out. Check the server connection and retry.');
    throw failure;
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', relay);
  }
}

const startup = (() => {
  const panel = document.getElementById('startup-status');
  if (!panel) return {
    ready() {},
    failed() {}
  };
  let finished = false;
  const failed = message => {
    panel.hidden = false;
    panel.setAttribute('role', 'alert');
    document.getElementById('startup-message').textContent = message;
    const loading = document.querySelector('.loading-start');
    if (loading) loading.textContent = 'Your consultation could not connect. Use Retry connection above.';
  };
  const timer = setTimeout(() => failed('The consultation has not connected. Check your server address or port forwarding, then retry.'), 15000);
  window.addEventListener('error', event => {
    if (!finished && (event.target?.tagName === 'SCRIPT' || event.error)) {
      failed('The page could not finish loading. Retry the connection or reopen the direct browser address.');
    }
  }, true);
  return {
    ready() {
      finished = true;
      clearTimeout(timer);
      panel.hidden = true;
    },
    failed(message) {
      clearTimeout(timer);
      failed(message);
    }
  };
})();
