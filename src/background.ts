import {
  buildRelayWsUrl,
  isRetryableReconnectError,
  isSkippableUrl,
  reconnectDelayMs,
} from './background-utils.js';
import type { BadgeKind, PersistedTab, TabSession, TabListEntry } from './types.js';

const DEFAULT_PORT = 18792;
const startedAt = Date.now();

const BADGE: Record<BadgeKind, { text: string; color: string }> = {
  on: { text: 'ON', color: '#16a34a' },
  off: { text: '', color: '#000000' },
  connecting: { text: '...', color: '#F59E0B' },
  error: { text: '!', color: '#B91C1C' },
};

let relayWs: WebSocket | null = null;
let relayConnectPromise: Promise<void> | null = null;
let nextSession = 1;

const tabs = new Map<number, TabSession>();
const tabBySession = new Map<string, number>();
const childSessionToTab = new Map<string, number>();
const pending = new Map<number, { resolve: (v: unknown) => void; reject: (e: Error) => void }>();
const tabOperationLocks = new Set<number>();
const reattachPending = new Set<number>();

let reconnectAttempt = 0;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

// --- Settings ---

async function getRelayPort(): Promise<number> {
  const stored = await chrome.storage.local.get(['relayPort']);
  const n = Number.parseInt(String(stored.relayPort || ''), 10);
  if (!Number.isFinite(n) || n <= 0 || n > 65535) return DEFAULT_PORT;
  return n;
}

async function getGatewayToken(): Promise<string> {
  const stored = await chrome.storage.local.get(['gatewayToken']);
  return String(stored.gatewayToken || '').trim();
}

async function isAutoAttachEnabled(): Promise<boolean> {
  const stored = await chrome.storage.local.get(['autoAttach']);
  return stored.autoAttach !== false;
}

// --- Badge ---

function setBadge(tabId: number, kind: BadgeKind): void {
  const cfg = BADGE[kind];
  void chrome.action.setBadgeText({ tabId, text: cfg.text });
  void chrome.action.setBadgeBackgroundColor({ tabId, color: cfg.color });
  void chrome.action.setBadgeTextColor({ tabId, color: '#FFFFFF' }).catch(() => {});
}

function updateGlobalBadge(): void {
  const attachedCount = [...tabs.values()].filter((t) => t.state === 'connected').length;
  const wsConnected = relayWs && relayWs.readyState === WebSocket.OPEN;

  const text = attachedCount > 0 ? String(attachedCount) : '';
  let color: string;
  if (wsConnected && attachedCount > 0) {
    color = '#16a34a'; // green
  } else if (wsConnected) {
    color = '#F59E0B'; // yellow
  } else {
    color = '#B91C1C'; // red
  }

  void chrome.action.setBadgeText({ text });
  void chrome.action.setBadgeBackgroundColor({ color });
  void chrome.action.setBadgeTextColor({ color: '#FFFFFF' }).catch(() => {});
}

// --- State Persistence ---

async function persistState(): Promise<void> {
  try {
    const tabEntries: PersistedTab[] = [];
    for (const [tabId, tab] of tabs.entries()) {
      if (tab.state === 'connected' && tab.sessionId && tab.targetId) {
        tabEntries.push({
          tabId,
          sessionId: tab.sessionId,
          targetId: tab.targetId,
          attachOrder: tab.attachOrder ?? 0,
        });
      }
    }
    await chrome.storage.session.set({ persistedTabs: tabEntries, nextSession });
  } catch {
    // chrome.storage.session may not be available
  }
  updateGlobalBadge();
}

async function rehydrateState(): Promise<void> {
  try {
    const stored = await chrome.storage.session.get(['persistedTabs', 'nextSession']);
    if (stored.nextSession) {
      nextSession = Math.max(nextSession, stored.nextSession);
    }
    const entries: PersistedTab[] = stored.persistedTabs || [];

    for (const entry of entries) {
      tabs.set(entry.tabId, {
        state: 'connected',
        sessionId: entry.sessionId,
        targetId: entry.targetId,
        attachOrder: entry.attachOrder,
      });
      tabBySession.set(entry.sessionId, entry.tabId);
      setBadge(entry.tabId, 'on');
    }

    for (const entry of entries) {
      try {
        await chrome.tabs.get(entry.tabId);
        await chrome.debugger.sendCommand({ tabId: entry.tabId }, 'Runtime.evaluate', {
          expression: '1',
          returnByValue: true,
        });
      } catch {
        tabs.delete(entry.tabId);
        tabBySession.delete(entry.sessionId);
        setBadge(entry.tabId, 'off');
      }
    }
  } catch {
    // Ignore rehydration errors
  }
  updateGlobalBadge();
}

// --- WebSocket Relay ---

async function ensureRelayConnection(): Promise<void> {
  if (relayWs && relayWs.readyState === WebSocket.OPEN) return;
  if (relayConnectPromise) return await relayConnectPromise;

  relayConnectPromise = (async () => {
    const port = await getRelayPort();
    const gatewayToken = await getGatewayToken();
    const httpBase = `http://127.0.0.1:${port}`;
    const wsUrl = await buildRelayWsUrl(port, gatewayToken);

    try {
      await fetch(`${httpBase}/`, { method: 'HEAD', signal: AbortSignal.timeout(2000) });
    } catch (err) {
      throw new Error(`Relay server not reachable at ${httpBase} (${String(err)})`);
    }

    const ws = new WebSocket(wsUrl);
    relayWs = ws;

    await new Promise<void>((resolve, reject) => {
      const t = setTimeout(() => reject(new Error('WebSocket connect timeout')), 5000);
      ws.onopen = () => {
        clearTimeout(t);
        resolve();
      };
      ws.onerror = () => {
        clearTimeout(t);
        reject(new Error('WebSocket connect failed'));
      };
      ws.onclose = (ev) => {
        clearTimeout(t);
        reject(new Error(`WebSocket closed (${ev.code} ${ev.reason || 'no reason'})`));
      };
    });

    ws.onmessage = (event) => {
      if (ws !== relayWs) return;
      void whenReady(() => onRelayMessage(String(event.data || '')));
    };
    ws.onclose = () => {
      if (ws !== relayWs) return;
      onRelayClosed('closed');
    };
    ws.onerror = () => {
      if (ws !== relayWs) return;
      onRelayClosed('error');
    };
  })();

  try {
    await relayConnectPromise;
    reconnectAttempt = 0;
    updateGlobalBadge();
  } finally {
    relayConnectPromise = null;
  }
}

function onRelayClosed(reason: string): void {
  relayWs = null;

  for (const [id, p] of pending.entries()) {
    pending.delete(id);
    p.reject(new Error(`Relay disconnected (${reason})`));
  }

  reattachPending.clear();
  updateGlobalBadge();

  for (const [tabId, tab] of tabs.entries()) {
    if (tab.state === 'connected') {
      setBadge(tabId, 'connecting');
    }
  }

  scheduleReconnect();
}

function scheduleReconnect(): void {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }

  const delay = reconnectDelayMs(reconnectAttempt);
  reconnectAttempt++;

  console.log(`Scheduling reconnect attempt ${reconnectAttempt} in ${Math.round(delay)}ms`);

  reconnectTimer = setTimeout(async () => {
    reconnectTimer = null;
    try {
      await ensureRelayConnection();
      reconnectAttempt = 0;
      console.log('Reconnected successfully');
      await reannounceAttachedTabs();
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      console.warn(`Reconnect attempt ${reconnectAttempt} failed: ${message}`);
      if (!isRetryableReconnectError(err)) return;
      scheduleReconnect();
    }
  }, delay);
}

function cancelReconnect(): void {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  reconnectAttempt = 0;
}

async function reannounceAttachedTabs(): Promise<void> {
  for (const [tabId, tab] of tabs.entries()) {
    if (tab.state !== 'connected' || !tab.sessionId || !tab.targetId) continue;

    try {
      await chrome.debugger.sendCommand({ tabId }, 'Runtime.evaluate', {
        expression: '1',
        returnByValue: true,
      });
    } catch {
      tabs.delete(tabId);
      if (tab.sessionId) tabBySession.delete(tab.sessionId);
      setBadge(tabId, 'off');
      continue;
    }

    try {
      const info = await chrome.debugger.sendCommand({ tabId }, 'Target.getTargetInfo') as {
        targetInfo?: Record<string, unknown>;
      };

      sendToRelay({
        method: 'forwardCDPEvent',
        params: {
          method: 'Target.attachedToTarget',
          params: {
            sessionId: tab.sessionId,
            targetInfo: { ...info?.targetInfo, attached: true },
            waitingForDebugger: false,
          },
        },
      });

      setBadge(tabId, 'on');
    } catch {
      setBadge(tabId, 'on');
    }
  }

  await persistState();
}

function sendToRelay(payload: Record<string, unknown>): void {
  const ws = relayWs;
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    throw new Error('Relay not connected');
  }
  ws.send(JSON.stringify(payload));
}

// --- Tab Attach / Detach ---

async function attachTab(
  tabId: number,
  opts: { skipAttachedEvent?: boolean } = {},
): Promise<{ sessionId: string; targetId: string }> {
  const debuggee = { tabId };
  await chrome.debugger.attach(debuggee, '1.3');
  await chrome.debugger.sendCommand(debuggee, 'Page.enable').catch(() => {});

  const info = (await chrome.debugger.sendCommand(debuggee, 'Target.getTargetInfo')) as {
    targetInfo?: { targetId?: string; url?: string; title?: string; [k: string]: unknown };
  };
  const targetInfo = info?.targetInfo;
  const targetId = String(targetInfo?.targetId || '').trim();
  if (!targetId) throw new Error('Target.getTargetInfo returned no targetId');

  const sid = nextSession++;
  const sessionId = `cb-tab-${sid}`;

  const tabInfo = await chrome.tabs.get(tabId).catch(() => null);

  tabs.set(tabId, {
    state: 'connected',
    sessionId,
    targetId,
    attachOrder: sid,
    url: tabInfo?.url || targetInfo?.url as string || '',
    title: tabInfo?.title || targetInfo?.title as string || '',
    attachedAt: Date.now(),
  });
  tabBySession.set(sessionId, tabId);

  if (!opts.skipAttachedEvent) {
    try {
      sendToRelay({
        method: 'forwardCDPEvent',
        params: {
          method: 'Target.attachedToTarget',
          params: {
            sessionId,
            targetInfo: { ...targetInfo, attached: true },
            waitingForDebugger: false,
          },
        },
      });
    } catch {
      // Relay may be down — that's OK, we'll reannounce on reconnect
    }
  }

  setBadge(tabId, 'on');
  await persistState();

  return { sessionId, targetId };
}

async function detachTab(tabId: number, reason: string): Promise<void> {
  const tab = tabs.get(tabId);

  for (const [childSessionId, parentTabId] of childSessionToTab.entries()) {
    if (parentTabId === tabId) {
      try {
        sendToRelay({
          method: 'forwardCDPEvent',
          params: {
            method: 'Target.detachedFromTarget',
            params: { sessionId: childSessionId, reason: 'parent_detached' },
          },
        });
      } catch {
        // Relay may be down
      }
      childSessionToTab.delete(childSessionId);
    }
  }

  if (tab?.sessionId && tab?.targetId) {
    try {
      sendToRelay({
        method: 'forwardCDPEvent',
        params: {
          method: 'Target.detachedFromTarget',
          params: { sessionId: tab.sessionId, targetId: tab.targetId, reason },
        },
      });
    } catch {
      // Relay may be down
    }
  }

  if (tab?.sessionId) tabBySession.delete(tab.sessionId);
  tabs.delete(tabId);

  try {
    await chrome.debugger.detach({ tabId });
  } catch {
    // May already be detached
  }

  setBadge(tabId, 'off');
  await persistState();
}

// --- Auto-Attach Logic ---

async function autoAttachAllTabs(): Promise<void> {
  if (!(await isAutoAttachEnabled())) return;

  const allTabs = await chrome.tabs.query({});
  const ownOptionsUrl = chrome.runtime.getURL('options.html');

  for (const tab of allTabs) {
    if (!tab.id) continue;
    if (tabs.has(tab.id)) continue;
    if (isSkippableUrl(tab.url)) continue;
    if (tab.url === ownOptionsUrl) continue;
    if (tabOperationLocks.has(tab.id)) continue;

    tabOperationLocks.add(tab.id);
    try {
      await attachTab(tab.id);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      console.warn(`Auto-attach failed for tab ${tab.id} (${tab.url}): ${msg}`);
    } finally {
      tabOperationLocks.delete(tab.id);
    }
  }
}

async function autoAttachTab(tabId: number, url?: string): Promise<void> {
  if (!(await isAutoAttachEnabled())) return;
  if (tabs.has(tabId)) return;
  if (isSkippableUrl(url)) return;
  if (tabOperationLocks.has(tabId)) return;
  if (reattachPending.has(tabId)) return;

  const ownOptionsUrl = chrome.runtime.getURL('options.html');
  if (url === ownOptionsUrl) return;

  // Ensure relay is connected before attaching
  try {
    await ensureRelayConnection();
  } catch {
    return;
  }

  tabOperationLocks.add(tabId);
  try {
    await attachTab(tabId);
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.warn(`Auto-attach failed for tab ${tabId}: ${msg}`);
  } finally {
    tabOperationLocks.delete(tabId);
  }
}

// --- Action Click (toggle all attach/detach) ---

async function onActionClicked(): Promise<void> {
  const attachedCount = [...tabs.values()].filter((t) => t.state === 'connected').length;

  if (attachedCount > 0) {
    // Detach all
    for (const [tabId] of [...tabs.entries()]) {
      await detachTab(tabId, 'toggle');
    }
  } else {
    // Attach all
    cancelReconnect();
    try {
      await ensureRelayConnection();
      await autoAttachAllTabs();
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      console.warn('Attach all failed:', msg);
    }
  }
}

// --- Relay Message Handling ---

async function onRelayMessage(text: string): Promise<void> {
  let msg: Record<string, unknown>;
  try {
    msg = JSON.parse(text);
  } catch {
    return;
  }

  if (msg.method === 'ping') {
    try {
      sendToRelay({ method: 'pong' });
    } catch {
      // ignore
    }
    return;
  }

  if (typeof msg.id === 'number' && (msg.result !== undefined || msg.error !== undefined)) {
    const p = pending.get(msg.id);
    if (!p) return;
    pending.delete(msg.id);
    if (msg.error) p.reject(new Error(String(msg.error)));
    else p.resolve(msg.result);
    return;
  }

  if (typeof msg.id === 'number' && msg.method === 'forwardCDPCommand') {
    try {
      const result = await handleForwardCdpCommand(msg);
      sendToRelay({ id: msg.id, result });
    } catch (err) {
      sendToRelay({ id: msg.id, error: err instanceof Error ? err.message : String(err) });
    }
  }
}

// --- CDP Command Router ---

function getTabBySessionId(sessionId: string): { tabId: number; kind: string } | null {
  const direct = tabBySession.get(sessionId);
  if (direct) return { tabId: direct, kind: 'main' };
  const child = childSessionToTab.get(sessionId);
  if (child) return { tabId: child, kind: 'child' };
  return null;
}

function getTabByTargetId(targetId: string): number | null {
  for (const [tabId, tab] of tabs.entries()) {
    if (tab.targetId === targetId) return tabId;
  }
  return null;
}

function resolveTabId(msg: Record<string, unknown>): number | null {
  const params = msg.params as Record<string, unknown> | undefined;
  const sessionId = typeof params?.sessionId === 'string' ? params.sessionId : undefined;
  const innerParams = params?.params as Record<string, unknown> | undefined;
  const targetId = typeof innerParams?.targetId === 'string' ? innerParams.targetId : undefined;

  const bySession = sessionId ? getTabBySessionId(sessionId) : null;
  if (bySession) return bySession.tabId;

  if (targetId) {
    const byTarget = getTabByTargetId(targetId);
    if (byTarget) return byTarget;
  }

  // Fall back to any connected tab
  for (const [id, tab] of tabs.entries()) {
    if (tab.state === 'connected') return id;
  }
  return null;
}

async function handleForwardCdpCommand(msg: Record<string, unknown>): Promise<unknown> {
  const params = msg.params as Record<string, unknown>;
  const method = String(params?.method || '').trim();
  const cmdParams = (params?.params || undefined) as Record<string, unknown> | undefined;
  const sessionId = typeof params?.sessionId === 'string' ? params.sessionId : undefined;

  // --- Custom Commands ---

  if (method === 'Tab.list') {
    return handleTabList();
  }

  if (method === 'Tab.attachAll') {
    await autoAttachAllTabs();
    return handleTabList();
  }

  if (method === 'Tab.getStatus') {
    return handleGetStatus();
  }

  if (method.startsWith('Cookie.')) {
    return handleCookieCommand(method, cmdParams);
  }

  if (method.startsWith('Download.')) {
    return handleDownloadCommand(method, cmdParams);
  }

  // --- Standard CDP ---

  const tabId = resolveTabId(msg);
  if (!tabId) throw new Error(`No attached tab for method ${method}`);

  const debuggee: chrome.debugger.Debuggee = { tabId };

  if (method === 'Runtime.enable') {
    try {
      await chrome.debugger.sendCommand(debuggee, 'Runtime.disable');
      await new Promise((r) => setTimeout(r, 50));
    } catch {
      // ignore
    }
    return await chrome.debugger.sendCommand(debuggee, 'Runtime.enable', cmdParams);
  }

  if (method === 'Target.createTarget') {
    const url = typeof cmdParams?.url === 'string' ? cmdParams.url : 'about:blank';
    const tab = await chrome.tabs.create({ url, active: false });
    if (!tab.id) throw new Error('Failed to create tab');
    await new Promise((r) => setTimeout(r, 100));
    const attached = await attachTab(tab.id);
    return { targetId: attached.targetId };
  }

  if (method === 'Target.closeTarget') {
    const target = typeof cmdParams?.targetId === 'string' ? cmdParams.targetId : '';
    const toClose = target ? getTabByTargetId(target) : tabId;
    if (!toClose) return { success: false };
    try {
      await chrome.tabs.remove(toClose);
    } catch {
      return { success: false };
    }
    return { success: true };
  }

  if (method === 'Target.activateTarget') {
    const target = typeof cmdParams?.targetId === 'string' ? cmdParams.targetId : '';
    const toActivate = target ? getTabByTargetId(target) : tabId;
    if (!toActivate) return {};
    const tab = await chrome.tabs.get(toActivate).catch(() => null);
    if (!tab) return {};
    if (tab.windowId) {
      await chrome.windows.update(tab.windowId, { focused: true }).catch(() => {});
    }
    await chrome.tabs.update(toActivate, { active: true }).catch(() => {});
    return {};
  }

  const tabState = tabs.get(tabId);
  const mainSessionId = tabState?.sessionId;
  const debuggerSession =
    sessionId && mainSessionId && sessionId !== mainSessionId
      ? { ...debuggee, sessionId }
      : debuggee;

  return await chrome.debugger.sendCommand(debuggerSession, method, cmdParams);
}

// --- Custom Command Handlers ---

function handleTabList(): TabListEntry[] {
  const result: TabListEntry[] = [];
  for (const [tabId, tab] of tabs.entries()) {
    if (tab.state === 'connected' && tab.sessionId && tab.targetId) {
      result.push({
        tabId,
        sessionId: tab.sessionId,
        targetId: tab.targetId,
        url: tab.url || '',
        title: tab.title || '',
        status: tab.state,
        attachedAt: tab.attachedAt || 0,
      });
    }
  }
  return result;
}

function handleGetStatus(): Record<string, unknown> {
  return {
    wsState: relayWs && relayWs.readyState === WebSocket.OPEN
      ? 'connected'
      : relayConnectPromise
        ? 'connecting'
        : 'disconnected',
    attachedCount: [...tabs.values()].filter((t) => t.state === 'connected').length,
    tabs: handleTabList(),
    uptime: Date.now() - startedAt,
  };
}

async function handleCookieCommand(
  method: string,
  params?: Record<string, unknown>,
): Promise<unknown> {
  switch (method) {
    case 'Cookie.getAll': {
      const details: chrome.cookies.GetAllDetails = {};
      if (typeof params?.domain === 'string') details.domain = params.domain;
      if (typeof params?.url === 'string') details.url = params.url;
      if (typeof params?.name === 'string') details.name = params.name;
      return await chrome.cookies.getAll(details);
    }

    case 'Cookie.set': {
      if (typeof params?.url !== 'string') throw new Error('Cookie.set requires url');
      const cookie: chrome.cookies.SetDetails = { url: params.url as string };
      if (typeof params?.name === 'string') cookie.name = params.name;
      if (typeof params?.value === 'string') cookie.value = params.value;
      if (typeof params?.domain === 'string') cookie.domain = params.domain;
      if (typeof params?.path === 'string') cookie.path = params.path;
      if (typeof params?.secure === 'boolean') cookie.secure = params.secure;
      if (typeof params?.httpOnly === 'boolean') cookie.httpOnly = params.httpOnly;
      if (typeof params?.sameSite === 'string')
        cookie.sameSite = params.sameSite as chrome.cookies.SameSiteStatus;
      if (typeof params?.expirationDate === 'number') cookie.expirationDate = params.expirationDate;
      return await chrome.cookies.set(cookie);
    }

    case 'Cookie.remove': {
      if (typeof params?.url !== 'string') throw new Error('Cookie.remove requires url');
      if (typeof params?.name !== 'string') throw new Error('Cookie.remove requires name');
      return await chrome.cookies.remove({
        url: params.url as string,
        name: params.name as string,
      });
    }

    case 'Cookie.export': {
      const details: chrome.cookies.GetAllDetails = {};
      if (typeof params?.domain === 'string') details.domain = params.domain;
      if (typeof params?.url === 'string') details.url = params.url;
      const cookies = await chrome.cookies.getAll(details);
      return { cookies, exportedAt: Date.now() };
    }

    case 'Cookie.import': {
      const cookies = params?.cookies;
      if (!Array.isArray(cookies)) throw new Error('Cookie.import requires cookies array');
      const results: { success: boolean; name: string; error?: string }[] = [];
      for (const c of cookies) {
        try {
          if (typeof c.url !== 'string') throw new Error('Missing url');
          await chrome.cookies.set(c);
          results.push({ success: true, name: c.name || '' });
        } catch (err) {
          results.push({
            success: false,
            name: c.name || '',
            error: err instanceof Error ? err.message : String(err),
          });
        }
      }
      return { results };
    }

    default:
      throw new Error(`Unknown cookie command: ${method}`);
  }
}

async function handleDownloadCommand(
  method: string,
  params?: Record<string, unknown>,
): Promise<unknown> {
  switch (method) {
    case 'Download.start': {
      if (typeof params?.url !== 'string') throw new Error('Download.start requires url');
      const options: chrome.downloads.DownloadOptions = { url: params.url as string };
      if (typeof params?.filename === 'string') options.filename = params.filename;
      if (typeof params?.saveAs === 'boolean') options.saveAs = params.saveAs;
      const downloadId = await chrome.downloads.download(options);
      return { downloadId };
    }

    case 'Download.list': {
      const query: chrome.downloads.DownloadQuery = {};
      if (typeof params?.limit === 'number') query.limit = params.limit;
      const items = await chrome.downloads.search(query);
      return items.map((item) => ({
        id: item.id,
        url: item.url,
        filename: item.filename,
        state: item.state,
        bytesReceived: item.bytesReceived,
        totalBytes: item.totalBytes,
        startTime: item.startTime,
        endTime: item.endTime,
      }));
    }

    case 'Download.getStatus': {
      if (typeof params?.downloadId !== 'number') throw new Error('Download.getStatus requires downloadId');
      const items = await chrome.downloads.search({ id: params.downloadId as number });
      if (items.length === 0) throw new Error('Download not found');
      const item = items[0];
      return {
        id: item.id,
        url: item.url,
        filename: item.filename,
        state: item.state,
        bytesReceived: item.bytesReceived,
        totalBytes: item.totalBytes,
        startTime: item.startTime,
        endTime: item.endTime,
        error: item.error,
      };
    }

    case 'Download.cancel': {
      if (typeof params?.downloadId !== 'number') throw new Error('Download.cancel requires downloadId');
      await chrome.downloads.cancel(params.downloadId as number);
      return { success: true };
    }

    case 'Download.open': {
      if (typeof params?.downloadId !== 'number') throw new Error('Download.open requires downloadId');
      await chrome.downloads.open(params.downloadId as number);
      return { success: true };
    }

    default:
      throw new Error(`Unknown download command: ${method}`);
  }
}

// --- Debugger Event Handlers ---

function onDebuggerEvent(
  source: chrome.debugger.Debuggee,
  method: string,
  params?: Record<string, unknown>,
): void {
  const tabId = source.tabId;
  if (!tabId) return;
  const tab = tabs.get(tabId);
  if (!tab?.sessionId) return;

  if (method === 'Target.attachedToTarget' && params?.sessionId) {
    childSessionToTab.set(String(params.sessionId), tabId);
  }

  if (method === 'Target.detachedFromTarget' && params?.sessionId) {
    childSessionToTab.delete(String(params.sessionId));
  }

  try {
    sendToRelay({
      method: 'forwardCDPEvent',
      params: {
        sessionId: (source as Record<string, unknown>).sessionId as string || tab.sessionId,
        method,
        params,
      },
    });
  } catch {
    // Relay may be down
  }
}

async function onDebuggerDetach(
  source: chrome.debugger.Debuggee,
  reason: string,
): Promise<void> {
  const tabId = source.tabId;
  if (!tabId) return;
  if (!tabs.has(tabId)) return;

  if (reason === 'canceled_by_user' || reason === 'replaced_with_devtools') {
    void detachTab(tabId, reason);
    return;
  }

  let tabInfo: chrome.tabs.Tab | undefined;
  try {
    tabInfo = await chrome.tabs.get(tabId);
  } catch {
    void detachTab(tabId, reason);
    return;
  }

  if (isSkippableUrl(tabInfo.url)) {
    void detachTab(tabId, reason);
    return;
  }

  if (reattachPending.has(tabId)) return;

  const oldTab = tabs.get(tabId);
  const oldSessionId = oldTab?.sessionId;
  const oldTargetId = oldTab?.targetId;

  if (oldSessionId) tabBySession.delete(oldSessionId);
  tabs.delete(tabId);
  for (const [childSessionId, parentTabId] of childSessionToTab.entries()) {
    if (parentTabId === tabId) childSessionToTab.delete(childSessionId);
  }

  if (oldSessionId && oldTargetId) {
    try {
      sendToRelay({
        method: 'forwardCDPEvent',
        params: {
          method: 'Target.detachedFromTarget',
          params: { sessionId: oldSessionId, targetId: oldTargetId, reason: 'navigation-reattach' },
        },
      });
    } catch {
      // Relay may be down
    }
  }

  reattachPending.add(tabId);
  setBadge(tabId, 'connecting');
  updateGlobalBadge();

  const delays = [300, 700, 1500];
  for (let attempt = 0; attempt < delays.length; attempt++) {
    await new Promise((r) => setTimeout(r, delays[attempt]));

    if (!reattachPending.has(tabId)) return;

    try {
      await chrome.tabs.get(tabId);
    } catch {
      reattachPending.delete(tabId);
      setBadge(tabId, 'off');
      updateGlobalBadge();
      return;
    }

    if (!relayWs || relayWs.readyState !== WebSocket.OPEN) {
      // Still re-attach even without relay — we'll announce on reconnect
      try {
        await attachTab(tabId, { skipAttachedEvent: true });
        reattachPending.delete(tabId);
        return;
      } catch {
        // continue retries
      }
    }

    try {
      await attachTab(tabId);
      reattachPending.delete(tabId);
      return;
    } catch {
      // continue retries
    }
  }

  reattachPending.delete(tabId);
  setBadge(tabId, 'off');
  updateGlobalBadge();
}

// --- Tab Lifecycle Listeners ---

chrome.tabs.onCreated.addListener((tab) =>
  void whenReady(() => {
    if (tab.id && tab.url && !isSkippableUrl(tab.url)) {
      void autoAttachTab(tab.id, tab.url);
    }
  }),
);

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) =>
  void whenReady(() => {
    if (changeInfo.status === 'loading' && tab.url && !tabs.has(tabId)) {
      void autoAttachTab(tabId, tab.url);
    }
    // Update stored URL/title
    const existing = tabs.get(tabId);
    if (existing) {
      if (changeInfo.url) existing.url = changeInfo.url;
      if (changeInfo.title) existing.title = changeInfo.title;
    }
  }),
);

chrome.tabs.onRemoved.addListener((tabId) =>
  void whenReady(() => {
    reattachPending.delete(tabId);
    if (!tabs.has(tabId)) return;
    const tab = tabs.get(tabId);
    if (tab?.sessionId) tabBySession.delete(tab.sessionId);
    tabs.delete(tabId);
    for (const [childSessionId, parentTabId] of childSessionToTab.entries()) {
      if (parentTabId === tabId) childSessionToTab.delete(childSessionId);
    }
    if (tab?.sessionId && tab?.targetId) {
      try {
        sendToRelay({
          method: 'forwardCDPEvent',
          params: {
            method: 'Target.detachedFromTarget',
            params: { sessionId: tab.sessionId, targetId: tab.targetId, reason: 'tab_closed' },
          },
        });
      } catch {
        // Relay may be down
      }
    }
    void persistState();
  }),
);

chrome.tabs.onReplaced.addListener((addedTabId, removedTabId) =>
  void whenReady(() => {
    const tab = tabs.get(removedTabId);
    if (!tab) return;
    tabs.delete(removedTabId);
    tabs.set(addedTabId, tab);
    if (tab.sessionId) {
      tabBySession.set(tab.sessionId, addedTabId);
    }
    for (const [childSessionId, parentTabId] of childSessionToTab.entries()) {
      if (parentTabId === removedTabId) {
        childSessionToTab.set(childSessionId, addedTabId);
      }
    }
    setBadge(addedTabId, 'on');
    void persistState();
  }),
);

// --- Debugger Listeners ---

chrome.debugger.onEvent.addListener(
  (source: chrome.debugger.Debuggee, method: string, params?: object) =>
    void whenReady(() => onDebuggerEvent(source, method, params as Record<string, unknown>)),
);
chrome.debugger.onDetach.addListener(
  (source: chrome.debugger.Debuggee, reason: string) =>
    void whenReady(() => onDebuggerDetach(source, reason)),
);

// --- Action Click ---

chrome.action.onClicked.addListener(() => void whenReady(() => onActionClicked()));

// --- Navigation Badge Refresh ---

chrome.webNavigation.onCompleted.addListener(({ tabId, frameId }) =>
  void whenReady(() => {
    if (frameId !== 0) return;
    const tab = tabs.get(tabId);
    if (tab?.state === 'connected') {
      setBadge(tabId, relayWs && relayWs.readyState === WebSocket.OPEN ? 'on' : 'connecting');
    }
  }),
);

chrome.tabs.onActivated.addListener(({ tabId }) =>
  void whenReady(() => {
    const tab = tabs.get(tabId);
    if (tab?.state === 'connected') {
      setBadge(tabId, relayWs && relayWs.readyState === WebSocket.OPEN ? 'on' : 'connecting');
    }
    updateGlobalBadge();
  }),
);

// --- Download Auto-Accept ---

chrome.downloads.onDeterminingFilename.addListener((item, suggest) => {
  suggest({ filename: item.filename, conflictAction: 'uniquify' });
});

// --- Install ---

chrome.runtime.onInstalled.addListener(() => {
  void chrome.runtime.openOptionsPage();
});

// --- Keepalive Alarm ---

chrome.alarms.create('relay-keepalive', { periodInMinutes: 0.4 }); // ~24s

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name !== 'relay-keepalive') return;
  await initPromise;

  updateGlobalBadge();

  // Auto-attach any new tabs that appeared
  if (await isAutoAttachEnabled()) {
    const allTabs = await chrome.tabs.query({});
    for (const tab of allTabs) {
      if (tab.id && !tabs.has(tab.id) && !isSkippableUrl(tab.url)) {
        void autoAttachTab(tab.id, tab.url);
      }
    }
  }

  // Reconnect if relay is down
  if (!relayWs || relayWs.readyState !== WebSocket.OPEN) {
    if (!relayConnectPromise && !reconnectTimer) {
      console.log('Keepalive: WebSocket unhealthy, triggering reconnect');
      await ensureRelayConnection().catch(() => {
        if (!reconnectTimer) scheduleReconnect();
      });
    }
  }
});

// --- Message Handler (options page + status) ---

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type === 'getStatus') {
    sendResponse(handleGetStatus());
    return false;
  }

  if (msg?.type === 'relayCheck') {
    const { url, token } = msg;
    const headers: Record<string, string> = token ? { 'x-openclaw-relay-token': token } : {};
    fetch(url, { method: 'GET', headers, signal: AbortSignal.timeout(2000) })
      .then(async (res) => {
        const contentType = String(res.headers.get('content-type') || '');
        let json = null;
        if (contentType.includes('application/json')) {
          try {
            json = await res.json();
          } catch {
            json = null;
          }
        }
        sendResponse({ status: res.status, ok: res.ok, contentType, json });
      })
      .catch((err) => sendResponse({ status: 0, ok: false, error: String(err) }));
    return true; // async response
  }

  return false;
});

// --- Initialization ---

const initPromise = rehydrateState();

initPromise.then(async () => {
  try {
    await ensureRelayConnection();
    reconnectAttempt = 0;
    if (tabs.size > 0) {
      await reannounceAttachedTabs();
    }
    await autoAttachAllTabs();
  } catch {
    scheduleReconnect();
    // Still try to attach tabs even without relay
    if (await isAutoAttachEnabled()) {
      try {
        await autoAttachAllTabs();
      } catch {
        // Best effort
      }
    }
  }
});

async function whenReady<T>(fn: () => T | Promise<T>): Promise<T> {
  await initPromise;
  return fn();
}
