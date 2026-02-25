import { deriveRelayToken } from './background-utils.js';
import { classifyRelayCheckException, classifyRelayCheckResponse } from './options-validation.js';

const DEFAULT_PORT = 18792;

function clampPort(value: string | number | undefined): number {
  const n = Number.parseInt(String(value || ''), 10);
  if (!Number.isFinite(n)) return DEFAULT_PORT;
  if (n <= 0 || n > 65535) return DEFAULT_PORT;
  return n;
}

function updateRelayUrl(port: number): void {
  const el = document.getElementById('relay-url');
  if (!el) return;
  el.textContent = `http://127.0.0.1:${port}/`;
}

function setStatus(kind: string, message: string): void {
  const status = document.getElementById('status');
  if (!status) return;
  status.dataset.kind = kind || '';
  status.textContent = message || '';
}

async function checkRelayReachable(port: number, token: string): Promise<void> {
  const url = `http://127.0.0.1:${port}/json/version`;
  const trimmedToken = String(token || '').trim();
  if (!trimmedToken) {
    setStatus('error', 'Gateway token required. Save your gateway token to connect.');
    return;
  }
  try {
    const relayToken = await deriveRelayToken(trimmedToken, port);
    const res = await chrome.runtime.sendMessage({
      type: 'relayCheck',
      url,
      token: relayToken,
    });
    const result = classifyRelayCheckResponse(res, port);
    if (result.action === 'throw') throw new Error(result.error);
    setStatus(result.kind!, result.message!);
  } catch (err) {
    const result = classifyRelayCheckException(err, port);
    setStatus(result.kind, result.message);
  }
}

async function updateStatusPanel(): Promise<void> {
  try {
    const res = await chrome.runtime.sendMessage({ type: 'getStatus' });
    if (!res) return;

    const wsDot = document.getElementById('ws-dot');
    const wsStatus = document.getElementById('ws-status');
    const tabsDot = document.getElementById('tabs-dot');
    const tabsStatus = document.getElementById('tabs-status');

    if (wsDot && wsStatus) {
      wsDot.className = 'status-dot';
      if (res.wsState === 'connected') {
        wsDot.classList.add('green');
        wsStatus.textContent = 'WebSocket: connected';
      } else if (res.wsState === 'connecting') {
        wsDot.classList.add('yellow');
        wsStatus.textContent = 'WebSocket: connecting...';
      } else {
        wsDot.classList.add('red');
        wsStatus.textContent = 'WebSocket: disconnected';
      }
    }

    if (tabsDot && tabsStatus) {
      tabsDot.className = 'status-dot';
      if (res.attachedCount > 0) {
        tabsDot.classList.add('green');
        tabsStatus.textContent = `Tabs: ${res.attachedCount} attached`;
      } else {
        tabsDot.classList.add('yellow');
        tabsStatus.textContent = 'Tabs: none attached';
      }
    }
  } catch {
    // Extension context may be invalidated
  }
}

async function load(): Promise<void> {
  const stored = await chrome.storage.local.get([
    'relayPort',
    'gatewayToken',
    'autoAttach',
    'downloadDirectory',
  ]);
  const port = clampPort(stored.relayPort);
  const token = String(stored.gatewayToken || '').trim();
  const autoAttach = stored.autoAttach !== false;
  const downloadDir = String(stored.downloadDirectory || '');

  (document.getElementById('port') as HTMLInputElement).value = String(port);
  (document.getElementById('token') as HTMLInputElement).value = token;
  (document.getElementById('auto-attach') as HTMLInputElement).checked = autoAttach;
  (document.getElementById('download-dir') as HTMLInputElement).value = downloadDir;

  updateRelayUrl(port);
  await checkRelayReachable(port, token);
  await updateStatusPanel();
}

async function save(): Promise<void> {
  const portInput = document.getElementById('port') as HTMLInputElement;
  const tokenInput = document.getElementById('token') as HTMLInputElement;
  const autoAttachInput = document.getElementById('auto-attach') as HTMLInputElement;
  const downloadDirInput = document.getElementById('download-dir') as HTMLInputElement;

  const port = clampPort(portInput.value);
  const token = String(tokenInput.value || '').trim();
  const autoAttach = autoAttachInput.checked;
  const downloadDir = String(downloadDirInput.value || '').trim();

  await chrome.storage.local.set({
    relayPort: port,
    gatewayToken: token,
    autoAttach,
    downloadDirectory: downloadDir,
  });

  portInput.value = String(port);
  tokenInput.value = token;
  updateRelayUrl(port);
  await checkRelayReachable(port, token);
}

document.getElementById('save')!.addEventListener('click', () => void save());
document.getElementById('auto-attach')!.addEventListener('change', () => void save());

void load();
setInterval(() => void updateStatusPanel(), 3000);
