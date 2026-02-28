#!/usr/bin/env python3
"""
Patches the official OpenClaw browser relay extension to enable auto-attach.

What it does:
  - Adds cookies/downloads permissions and <all_urls> host permission
  - Auto-attaches Chrome debugger to ALL tabs (no manual clicking)
  - Adds custom commands: Tab.list, Tab.attachAll, Tab.getStatus,
    Cookie.getAll/set/remove/export/import, Download.start/list/getStatus/cancel/open
  - Adds status panel and auto-attach toggle to options page
  - Adds global badge showing attached tab count

Usage:
    python3 patch_auto_attach.py [path_to_extension_dir]

    If no path is given, auto-detects from:
      1. ~/.openclaw/browser/chrome-extension/
      2. $(openclaw browser extension path)
"""

import json
import os
import shutil
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Locate extension directory
# ---------------------------------------------------------------------------

def find_extension_dir(argv_path=None):
    if argv_path:
        p = Path(argv_path).expanduser().resolve()
        if p.is_dir() and (p / "manifest.json").exists():
            return p
        print(f"Error: {p} is not a valid extension directory (no manifest.json)")
        sys.exit(1)

    default = Path.home() / ".openclaw" / "browser" / "chrome-extension"
    if default.is_dir() and (default / "manifest.json").exists():
        return default

    try:
        import subprocess
        result = subprocess.run(
            ["openclaw", "browser", "extension", "path"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            p = Path(result.stdout.strip()).resolve()
            if p.is_dir() and (p / "manifest.json").exists():
                return p
    except Exception:
        pass

    print("Error: Could not find the OpenClaw extension directory.")
    print("Usage: python3 patch_auto_attach.py /path/to/chrome-extension/")
    print("   or: openclaw browser extension install  (then re-run this script)")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

def backup(ext_dir):
    backup_dir = ext_dir / ".backup-before-autoattach"
    if backup_dir.exists():
        shutil.rmtree(backup_dir)
    backup_dir.mkdir()
    for f in ["manifest.json", "background.js", "background-utils.js",
              "options.js", "options.html", "options-validation.js"]:
        src = ext_dir / f
        if src.exists():
            shutil.copy2(src, backup_dir / f)
    print(f"  Backed up original files to {backup_dir}/")
    return backup_dir


# ---------------------------------------------------------------------------
# 1. Patch manifest.json
# ---------------------------------------------------------------------------

def patch_manifest(ext_dir):
    path = ext_dir / "manifest.json"
    with open(path) as f:
        m = json.load(f)

    changed = False

    if "(Auto-Attach)" not in m.get("name", ""):
        m["name"] = m.get("name", "OpenClaw Browser Relay") + " (Auto-Attach)"
        changed = True

    m["description"] = (
        "Always-on OpenClaw browser relay — auto-attaches debugger to all tabs "
        "with zero interaction."
    )

    perms = m.setdefault("permissions", [])
    for p in ["cookies", "downloads"]:
        if p not in perms:
            perms.append(p)
            changed = True

    host_perms = m.setdefault("host_permissions", [])
    if "<all_urls>" not in host_perms:
        host_perms.insert(0, "<all_urls>")
        changed = True

    if m.get("action", {}).get("default_title", "") != "OpenClaw Browser Relay (auto-attach active)":
        m.setdefault("action", {})["default_title"] = "OpenClaw Browser Relay (auto-attach active)"
        changed = True

    with open(path, "w") as f:
        json.dump(m, f, indent=2)
        f.write("\n")

    print(f"  manifest.json: {'patched' if changed else 'already patched'}")


# ---------------------------------------------------------------------------
# 2. Patch background-utils.js — append isSkippableUrl
# ---------------------------------------------------------------------------

SKIPPABLE_URL_FN = '''
export function isSkippableUrl(url) {
  if (!url) return true
  return (
    url.startsWith('chrome://') ||
    url.startsWith('chrome-extension://') ||
    url.startsWith('about:') ||
    url.startsWith('devtools://')
  )
}
'''

def patch_background_utils(ext_dir):
    path = ext_dir / "background-utils.js"
    content = path.read_text()

    if "isSkippableUrl" in content:
        print("  background-utils.js: already has isSkippableUrl")
        return

    with open(path, "a") as f:
        f.write(SKIPPABLE_URL_FN)

    print("  background-utils.js: appended isSkippableUrl()")


# ---------------------------------------------------------------------------
# 3. Replace background.js
# ---------------------------------------------------------------------------

BACKGROUND_JS = r'''import { buildRelayWsUrl, isRetryableReconnectError, isSkippableUrl, reconnectDelayMs } from './background-utils.js'

const DEFAULT_PORT = 18792
const startedAt = Date.now()

const BADGE = {
  on: { text: 'ON', color: '#16a34a' },
  off: { text: '', color: '#000000' },
  connecting: { text: '\u2026', color: '#F59E0B' },
  error: { text: '!', color: '#B91C1C' },
}

/** @type {WebSocket|null} */
let relayWs = null
/** @type {Promise<void>|null} */
let relayConnectPromise = null
let relayGatewayToken = ''
/** @type {string|null} */
let relayConnectRequestId = null

let nextSession = 1

/** @type {Map<number, {state:'connecting'|'connected', sessionId?:string, targetId?:string, attachOrder?:number, url?:string, title?:string, attachedAt?:number}>} */
const tabs = new Map()
/** @type {Map<string, number>} */
const tabBySession = new Map()
/** @type {Map<string, number>} */
const childSessionToTab = new Map()

/** @type {Map<number, {resolve:(v:any)=>void, reject:(e:Error)=>void}>} */
const pending = new Map()

/** @type {Set<number>} */
const tabOperationLocks = new Set()
/** @type {Set<number>} */
const reattachPending = new Set()

let reconnectAttempt = 0
let reconnectTimer = null

// --- Settings ---

async function getRelayPort() {
  const stored = await chrome.storage.local.get(['relayPort'])
  const n = Number.parseInt(String(stored.relayPort || ''), 10)
  if (!Number.isFinite(n) || n <= 0 || n > 65535) return DEFAULT_PORT
  return n
}

async function getGatewayToken() {
  const stored = await chrome.storage.local.get(['gatewayToken'])
  return String(stored.gatewayToken || '').trim()
}

async function isAutoAttachEnabled() {
  const stored = await chrome.storage.local.get(['autoAttach'])
  return stored.autoAttach !== false
}

// --- Badge ---

function setBadge(tabId, kind) {
  const cfg = BADGE[kind]
  void chrome.action.setBadgeText({ tabId, text: cfg.text })
  void chrome.action.setBadgeBackgroundColor({ tabId, color: cfg.color })
  void chrome.action.setBadgeTextColor({ tabId, color: '#FFFFFF' }).catch(() => {})
}

function updateGlobalBadge() {
  const attachedCount = [...tabs.values()].filter((t) => t.state === 'connected').length
  const wsConnected = relayWs && relayWs.readyState === WebSocket.OPEN

  const text = attachedCount > 0 ? String(attachedCount) : ''
  let color
  if (wsConnected && attachedCount > 0) {
    color = '#16a34a'
  } else if (wsConnected) {
    color = '#F59E0B'
  } else {
    color = '#B91C1C'
  }

  void chrome.action.setBadgeText({ text })
  void chrome.action.setBadgeBackgroundColor({ color })
  void chrome.action.setBadgeTextColor({ color: '#FFFFFF' }).catch(() => {})
}

// --- State Persistence ---

async function persistState() {
  try {
    const tabEntries = []
    for (const [tabId, tab] of tabs.entries()) {
      if (tab.state === 'connected' && tab.sessionId && tab.targetId) {
        tabEntries.push({
          tabId,
          sessionId: tab.sessionId,
          targetId: tab.targetId,
          attachOrder: tab.attachOrder ?? 0,
        })
      }
    }
    await chrome.storage.session.set({ persistedTabs: tabEntries, nextSession })
  } catch {
    // chrome.storage.session may not be available
  }
  updateGlobalBadge()
}

async function rehydrateState() {
  try {
    const stored = await chrome.storage.session.get(['persistedTabs', 'nextSession'])
    if (stored.nextSession) {
      nextSession = Math.max(nextSession, stored.nextSession)
    }
    const entries = stored.persistedTabs || []
    for (const entry of entries) {
      tabs.set(entry.tabId, {
        state: 'connected',
        sessionId: entry.sessionId,
        targetId: entry.targetId,
        attachOrder: entry.attachOrder,
      })
      tabBySession.set(entry.sessionId, entry.tabId)
      setBadge(entry.tabId, 'on')
    }
    for (const entry of entries) {
      try {
        await chrome.tabs.get(entry.tabId)
        await chrome.debugger.sendCommand({ tabId: entry.tabId }, 'Runtime.evaluate', {
          expression: '1',
          returnByValue: true,
        })
      } catch {
        tabs.delete(entry.tabId)
        tabBySession.delete(entry.sessionId)
        setBadge(entry.tabId, 'off')
      }
    }
  } catch {
    // Ignore rehydration errors
  }
  updateGlobalBadge()
}

// --- WebSocket Relay ---

async function ensureRelayConnection() {
  if (relayWs && relayWs.readyState === WebSocket.OPEN) return
  if (relayConnectPromise) return await relayConnectPromise

  relayConnectPromise = (async () => {
    const port = await getRelayPort()
    const gatewayToken = await getGatewayToken()
    const httpBase = `http://127.0.0.1:${port}`
    const wsUrl = await buildRelayWsUrl(port, gatewayToken)

    try {
      await fetch(`${httpBase}/`, { method: 'HEAD', signal: AbortSignal.timeout(2000) })
    } catch (err) {
      throw new Error(`Relay server not reachable at ${httpBase} (${String(err)})`)
    }

    const ws = new WebSocket(wsUrl)
    relayWs = ws
    relayGatewayToken = gatewayToken

    ws.onmessage = (event) => {
      if (ws !== relayWs) return
      void whenReady(() => onRelayMessage(String(event.data || '')))
    }

    await new Promise((resolve, reject) => {
      const t = setTimeout(() => reject(new Error('WebSocket connect timeout')), 5000)
      ws.onopen = () => {
        clearTimeout(t)
        resolve()
      }
      ws.onerror = () => {
        clearTimeout(t)
        reject(new Error('WebSocket connect failed'))
      }
      ws.onclose = (ev) => {
        clearTimeout(t)
        reject(new Error(`WebSocket closed (${ev.code} ${ev.reason || 'no reason'})`))
      }
    })

    ws.onclose = () => {
      if (ws !== relayWs) return
      onRelayClosed('closed')
    }
    ws.onerror = () => {
      if (ws !== relayWs) return
      onRelayClosed('error')
    }
  })()

  try {
    await relayConnectPromise
    reconnectAttempt = 0
    updateGlobalBadge()
  } finally {
    relayConnectPromise = null
  }
}

function onRelayClosed(reason) {
  relayWs = null
  relayGatewayToken = ''
  relayConnectRequestId = null

  for (const [id, p] of pending.entries()) {
    pending.delete(id)
    p.reject(new Error(`Relay disconnected (${reason})`))
  }

  reattachPending.clear()
  updateGlobalBadge()

  for (const [tabId, tab] of tabs.entries()) {
    if (tab.state === 'connected') {
      setBadge(tabId, 'connecting')
    }
  }

  scheduleReconnect()
}

function scheduleReconnect() {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer)
    reconnectTimer = null
  }

  const delay = reconnectDelayMs(reconnectAttempt)
  reconnectAttempt++

  console.log(`Scheduling reconnect attempt ${reconnectAttempt} in ${Math.round(delay)}ms`)

  reconnectTimer = setTimeout(async () => {
    reconnectTimer = null
    try {
      await ensureRelayConnection()
      reconnectAttempt = 0
      console.log('Reconnected successfully')
      await reannounceAttachedTabs()
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err)
      console.warn(`Reconnect attempt ${reconnectAttempt} failed: ${message}`)
      if (!isRetryableReconnectError(err)) return
      scheduleReconnect()
    }
  }, delay)
}

function cancelReconnect() {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer)
    reconnectTimer = null
  }
  reconnectAttempt = 0
}

async function reannounceAttachedTabs() {
  for (const [tabId, tab] of tabs.entries()) {
    if (tab.state !== 'connected' || !tab.sessionId || !tab.targetId) continue

    try {
      await chrome.debugger.sendCommand({ tabId }, 'Runtime.evaluate', {
        expression: '1',
        returnByValue: true,
      })
    } catch {
      tabs.delete(tabId)
      if (tab.sessionId) tabBySession.delete(tab.sessionId)
      setBadge(tabId, 'off')
      continue
    }

    try {
      const info = /** @type {any} */ (
        await chrome.debugger.sendCommand({ tabId }, 'Target.getTargetInfo')
      )
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
      })
      setBadge(tabId, 'on')
    } catch {
      setBadge(tabId, 'on')
    }
  }

  await persistState()
}

function sendToRelay(payload) {
  const ws = relayWs
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    throw new Error('Relay not connected')
  }
  ws.send(JSON.stringify(payload))
}

function ensureGatewayHandshakeStarted(payload) {
  if (relayConnectRequestId) return
  const nonce = typeof payload?.nonce === 'string' ? payload.nonce.trim() : ''
  relayConnectRequestId = `ext-connect-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`
  sendToRelay({
    type: 'req',
    id: relayConnectRequestId,
    method: 'connect',
    params: {
      minProtocol: 3,
      maxProtocol: 3,
      client: {
        id: 'chrome-relay-extension',
        version: '1.0.0',
        platform: 'chrome-extension',
        mode: 'webchat',
      },
      role: 'operator',
      scopes: ['operator.read', 'operator.write'],
      caps: [],
      commands: [],
      nonce: nonce || undefined,
      auth: relayGatewayToken ? { token: relayGatewayToken } : undefined,
    },
  })
}

// --- Tab Attach / Detach ---

async function attachTab(tabId, opts = {}) {
  const debuggee = { tabId }
  await chrome.debugger.attach(debuggee, '1.3')
  await chrome.debugger.sendCommand(debuggee, 'Page.enable').catch(() => {})

  const info = /** @type {any} */ (await chrome.debugger.sendCommand(debuggee, 'Target.getTargetInfo'))
  const targetInfo = info?.targetInfo
  const targetId = String(targetInfo?.targetId || '').trim()
  if (!targetId) throw new Error('Target.getTargetInfo returned no targetId')

  const sid = nextSession++
  const sessionId = `cb-tab-${sid}`

  const tabInfo = await chrome.tabs.get(tabId).catch(() => null)

  tabs.set(tabId, {
    state: 'connected',
    sessionId,
    targetId,
    attachOrder: sid,
    url: tabInfo?.url || targetInfo?.url || '',
    title: tabInfo?.title || targetInfo?.title || '',
    attachedAt: Date.now(),
  })
  tabBySession.set(sessionId, tabId)

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
      })
    } catch {
      // Relay may be down — we'll reannounce on reconnect
    }
  }

  setBadge(tabId, 'on')
  await persistState()

  return { sessionId, targetId }
}

async function detachTab(tabId, reason) {
  const tab = tabs.get(tabId)

  for (const [childSessionId, parentTabId] of childSessionToTab.entries()) {
    if (parentTabId === tabId) {
      try {
        sendToRelay({
          method: 'forwardCDPEvent',
          params: {
            method: 'Target.detachedFromTarget',
            params: { sessionId: childSessionId, reason: 'parent_detached' },
          },
        })
      } catch {
        // Relay may be down
      }
      childSessionToTab.delete(childSessionId)
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
      })
    } catch {
      // Relay may be down
    }
  }

  if (tab?.sessionId) tabBySession.delete(tab.sessionId)
  tabs.delete(tabId)

  try {
    await chrome.debugger.detach({ tabId })
  } catch {
    // May already be detached
  }

  setBadge(tabId, 'off')
  await persistState()
}

// --- Auto-Attach Logic ---

async function autoAttachAllTabs() {
  if (!(await isAutoAttachEnabled())) return

  const allTabs = await chrome.tabs.query({})
  const ownOptionsUrl = chrome.runtime.getURL('options.html')

  for (const tab of allTabs) {
    if (!tab.id) continue
    if (tabs.has(tab.id)) continue
    if (isSkippableUrl(tab.url)) continue
    if (tab.url === ownOptionsUrl) continue
    if (tabOperationLocks.has(tab.id)) continue

    tabOperationLocks.add(tab.id)
    try {
      await attachTab(tab.id)
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err)
      console.warn(`Auto-attach failed for tab ${tab.id} (${tab.url}): ${msg}`)
    } finally {
      tabOperationLocks.delete(tab.id)
    }
  }
}

async function autoAttachTab(tabId, url) {
  if (!(await isAutoAttachEnabled())) return
  if (tabs.has(tabId)) return
  if (isSkippableUrl(url)) return
  if (tabOperationLocks.has(tabId)) return
  if (reattachPending.has(tabId)) return

  const ownOptionsUrl = chrome.runtime.getURL('options.html')
  if (url === ownOptionsUrl) return

  try {
    await ensureRelayConnection()
  } catch {
    return
  }

  tabOperationLocks.add(tabId)
  try {
    await attachTab(tabId)
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err)
    console.warn(`Auto-attach failed for tab ${tabId}: ${msg}`)
  } finally {
    tabOperationLocks.delete(tabId)
  }
}

// --- Action Click (toggle all attach/detach) ---

async function onActionClicked() {
  const attachedCount = [...tabs.values()].filter((t) => t.state === 'connected').length

  if (attachedCount > 0) {
    for (const [tabId] of [...tabs.entries()]) {
      await detachTab(tabId, 'toggle')
    }
  } else {
    cancelReconnect()
    try {
      await ensureRelayConnection()
      await autoAttachAllTabs()
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err)
      console.warn('Attach all failed:', msg)
    }
  }
}

// --- Relay Message Handling ---

async function onRelayMessage(text) {
  /** @type {any} */
  let msg
  try {
    msg = JSON.parse(text)
  } catch {
    return
  }

  if (msg && msg.type === 'event' && msg.event === 'connect.challenge') {
    try {
      ensureGatewayHandshakeStarted(msg.payload)
    } catch (err) {
      console.warn('gateway connect handshake start failed', err instanceof Error ? err.message : String(err))
      relayConnectRequestId = null
      const ws = relayWs
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.close(1008, 'gateway connect failed')
      }
    }
    return
  }

  if (msg && msg.type === 'res' && relayConnectRequestId && msg.id === relayConnectRequestId) {
    relayConnectRequestId = null
    if (!msg.ok) {
      const detail = msg?.error?.message || msg?.error || 'gateway connect failed'
      console.warn('gateway connect handshake rejected', String(detail))
      const ws = relayWs
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.close(1008, 'gateway connect failed')
      }
    }
    return
  }

  if (msg && msg.method === 'ping') {
    try {
      sendToRelay({ method: 'pong' })
    } catch {
      // ignore
    }
    return
  }

  if (msg && typeof msg.id === 'number' && (msg.result !== undefined || msg.error !== undefined)) {
    const p = pending.get(msg.id)
    if (!p) return
    pending.delete(msg.id)
    if (msg.error) p.reject(new Error(String(msg.error)))
    else p.resolve(msg.result)
    return
  }

  if (msg && typeof msg.id === 'number' && msg.method === 'forwardCDPCommand') {
    try {
      const result = await handleForwardCdpCommand(msg)
      sendToRelay({ id: msg.id, result })
    } catch (err) {
      sendToRelay({ id: msg.id, error: err instanceof Error ? err.message : String(err) })
    }
  }
}

// --- CDP Command Router ---

function getTabBySessionId(sessionId) {
  const direct = tabBySession.get(sessionId)
  if (direct) return { tabId: direct, kind: 'main' }
  const child = childSessionToTab.get(sessionId)
  if (child) return { tabId: child, kind: 'child' }
  return null
}

function getTabByTargetId(targetId) {
  for (const [tabId, tab] of tabs.entries()) {
    if (tab.targetId === targetId) return tabId
  }
  return null
}

function resolveTabId(msg) {
  const params = msg?.params
  const sessionId = typeof params?.sessionId === 'string' ? params.sessionId : undefined
  const innerParams = params?.params
  const targetId = typeof innerParams?.targetId === 'string' ? innerParams.targetId : undefined

  const bySession = sessionId ? getTabBySessionId(sessionId) : null
  if (bySession) return bySession.tabId

  if (targetId) {
    const byTarget = getTabByTargetId(targetId)
    if (byTarget) return byTarget
  }

  for (const [id, tab] of tabs.entries()) {
    if (tab.state === 'connected') return id
  }
  return null
}

async function handleForwardCdpCommand(msg) {
  const method = String(msg?.params?.method || '').trim()
  const params = msg?.params?.params || undefined
  const sessionId = typeof msg?.params?.sessionId === 'string' ? msg.params.sessionId : undefined

  // --- Custom Commands ---

  if (method === 'Tab.list') return handleTabList()
  if (method === 'Tab.attachAll') {
    await autoAttachAllTabs()
    return handleTabList()
  }
  if (method === 'Tab.getStatus') return handleGetStatus()
  if (method.startsWith('Cookie.')) return handleCookieCommand(method, params)
  if (method.startsWith('Download.')) return handleDownloadCommand(method, params)

  // --- Standard CDP ---

  const tabId = resolveTabId(msg)
  if (!tabId) throw new Error(`No attached tab for method ${method}`)

  const debuggee = { tabId }

  if (method === 'Runtime.enable') {
    try {
      await chrome.debugger.sendCommand(debuggee, 'Runtime.disable')
      await new Promise((r) => setTimeout(r, 50))
    } catch {
      // ignore
    }
    return await chrome.debugger.sendCommand(debuggee, 'Runtime.enable', params)
  }

  if (method === 'Target.createTarget') {
    const url = typeof params?.url === 'string' ? params.url : 'about:blank'
    const tab = await chrome.tabs.create({ url, active: false })
    if (!tab.id) throw new Error('Failed to create tab')
    await new Promise((r) => setTimeout(r, 100))
    const attached = await attachTab(tab.id)
    return { targetId: attached.targetId }
  }

  if (method === 'Target.closeTarget') {
    const target = typeof params?.targetId === 'string' ? params.targetId : ''
    const toClose = target ? getTabByTargetId(target) : tabId
    if (!toClose) return { success: false }
    try {
      await chrome.tabs.remove(toClose)
    } catch {
      return { success: false }
    }
    return { success: true }
  }

  if (method === 'Target.activateTarget') {
    const target = typeof params?.targetId === 'string' ? params.targetId : ''
    const toActivate = target ? getTabByTargetId(target) : tabId
    if (!toActivate) return {}
    const tab = await chrome.tabs.get(toActivate).catch(() => null)
    if (!tab) return {}
    if (tab.windowId) {
      await chrome.windows.update(tab.windowId, { focused: true }).catch(() => {})
    }
    await chrome.tabs.update(toActivate, { active: true }).catch(() => {})
    return {}
  }

  const tabState = tabs.get(tabId)
  const mainSessionId = tabState?.sessionId
  const debuggerSession =
    sessionId && mainSessionId && sessionId !== mainSessionId
      ? { ...debuggee, sessionId }
      : debuggee

  return await chrome.debugger.sendCommand(debuggerSession, method, params)
}

// --- Custom Command Handlers ---

function handleTabList() {
  const result = []
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
      })
    }
  }
  return result
}

function handleGetStatus() {
  return {
    wsState: relayWs && relayWs.readyState === WebSocket.OPEN
      ? 'connected'
      : relayConnectPromise
        ? 'connecting'
        : 'disconnected',
    attachedCount: [...tabs.values()].filter((t) => t.state === 'connected').length,
    tabs: handleTabList(),
    uptime: Date.now() - startedAt,
  }
}

async function handleCookieCommand(method, params) {
  switch (method) {
    case 'Cookie.getAll': {
      const details = {}
      if (typeof params?.domain === 'string') details.domain = params.domain
      if (typeof params?.url === 'string') details.url = params.url
      if (typeof params?.name === 'string') details.name = params.name
      return await chrome.cookies.getAll(details)
    }
    case 'Cookie.set': {
      if (typeof params?.url !== 'string') throw new Error('Cookie.set requires url')
      const cookie = { url: params.url }
      if (typeof params?.name === 'string') cookie.name = params.name
      if (typeof params?.value === 'string') cookie.value = params.value
      if (typeof params?.domain === 'string') cookie.domain = params.domain
      if (typeof params?.path === 'string') cookie.path = params.path
      if (typeof params?.secure === 'boolean') cookie.secure = params.secure
      if (typeof params?.httpOnly === 'boolean') cookie.httpOnly = params.httpOnly
      if (typeof params?.sameSite === 'string') cookie.sameSite = params.sameSite
      if (typeof params?.expirationDate === 'number') cookie.expirationDate = params.expirationDate
      return await chrome.cookies.set(cookie)
    }
    case 'Cookie.remove': {
      if (typeof params?.url !== 'string') throw new Error('Cookie.remove requires url')
      if (typeof params?.name !== 'string') throw new Error('Cookie.remove requires name')
      return await chrome.cookies.remove({ url: params.url, name: params.name })
    }
    case 'Cookie.export': {
      const details = {}
      if (typeof params?.domain === 'string') details.domain = params.domain
      if (typeof params?.url === 'string') details.url = params.url
      const cookies = await chrome.cookies.getAll(details)
      return { cookies, exportedAt: Date.now() }
    }
    case 'Cookie.import': {
      const cookies = params?.cookies
      if (!Array.isArray(cookies)) throw new Error('Cookie.import requires cookies array')
      const results = []
      for (const c of cookies) {
        try {
          if (typeof c.url !== 'string') throw new Error('Missing url')
          await chrome.cookies.set(c)
          results.push({ success: true, name: c.name || '' })
        } catch (err) {
          results.push({
            success: false,
            name: c.name || '',
            error: err instanceof Error ? err.message : String(err),
          })
        }
      }
      return { results }
    }
    default:
      throw new Error(`Unknown cookie command: ${method}`)
  }
}

async function handleDownloadCommand(method, params) {
  switch (method) {
    case 'Download.start': {
      if (typeof params?.url !== 'string') throw new Error('Download.start requires url')
      const options = { url: params.url }
      if (typeof params?.filename === 'string') options.filename = params.filename
      if (typeof params?.saveAs === 'boolean') options.saveAs = params.saveAs
      const downloadId = await chrome.downloads.download(options)
      return { downloadId }
    }
    case 'Download.list': {
      const query = {}
      if (typeof params?.limit === 'number') query.limit = params.limit
      const items = await chrome.downloads.search(query)
      return items.map((item) => ({
        id: item.id,
        url: item.url,
        filename: item.filename,
        state: item.state,
        bytesReceived: item.bytesReceived,
        totalBytes: item.totalBytes,
        startTime: item.startTime,
        endTime: item.endTime,
      }))
    }
    case 'Download.getStatus': {
      if (typeof params?.downloadId !== 'number') throw new Error('Download.getStatus requires downloadId')
      const items = await chrome.downloads.search({ id: params.downloadId })
      if (items.length === 0) throw new Error('Download not found')
      const item = items[0]
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
      }
    }
    case 'Download.cancel': {
      if (typeof params?.downloadId !== 'number') throw new Error('Download.cancel requires downloadId')
      await chrome.downloads.cancel(params.downloadId)
      return { success: true }
    }
    case 'Download.open': {
      if (typeof params?.downloadId !== 'number') throw new Error('Download.open requires downloadId')
      await chrome.downloads.open(params.downloadId)
      return { success: true }
    }
    default:
      throw new Error(`Unknown download command: ${method}`)
  }
}

// --- Debugger Event Handlers ---

function onDebuggerEvent(source, method, params) {
  const tabId = source.tabId
  if (!tabId) return
  const tab = tabs.get(tabId)
  if (!tab?.sessionId) return

  if (method === 'Target.attachedToTarget' && params?.sessionId) {
    childSessionToTab.set(String(params.sessionId), tabId)
  }
  if (method === 'Target.detachedFromTarget' && params?.sessionId) {
    childSessionToTab.delete(String(params.sessionId))
  }

  try {
    sendToRelay({
      method: 'forwardCDPEvent',
      params: {
        sessionId: source.sessionId || tab.sessionId,
        method,
        params,
      },
    })
  } catch {
    // Relay may be down
  }
}

async function onDebuggerDetach(source, reason) {
  const tabId = source.tabId
  if (!tabId) return
  if (!tabs.has(tabId)) return

  if (reason === 'canceled_by_user' || reason === 'replaced_with_devtools') {
    void detachTab(tabId, reason)
    return
  }

  let tabInfo
  try {
    tabInfo = await chrome.tabs.get(tabId)
  } catch {
    void detachTab(tabId, reason)
    return
  }

  if (isSkippableUrl(tabInfo.url)) {
    void detachTab(tabId, reason)
    return
  }

  if (reattachPending.has(tabId)) return

  const oldTab = tabs.get(tabId)
  const oldSessionId = oldTab?.sessionId
  const oldTargetId = oldTab?.targetId

  if (oldSessionId) tabBySession.delete(oldSessionId)
  tabs.delete(tabId)
  for (const [childSessionId, parentTabId] of childSessionToTab.entries()) {
    if (parentTabId === tabId) childSessionToTab.delete(childSessionId)
  }

  if (oldSessionId && oldTargetId) {
    try {
      sendToRelay({
        method: 'forwardCDPEvent',
        params: {
          method: 'Target.detachedFromTarget',
          params: { sessionId: oldSessionId, targetId: oldTargetId, reason: 'navigation-reattach' },
        },
      })
    } catch {
      // Relay may be down
    }
  }

  reattachPending.add(tabId)
  setBadge(tabId, 'connecting')
  updateGlobalBadge()

  const delays = [300, 700, 1500]
  for (let attempt = 0; attempt < delays.length; attempt++) {
    await new Promise((r) => setTimeout(r, delays[attempt]))
    if (!reattachPending.has(tabId)) return

    try {
      await chrome.tabs.get(tabId)
    } catch {
      reattachPending.delete(tabId)
      setBadge(tabId, 'off')
      updateGlobalBadge()
      return
    }

    // Re-attach even without relay — we'll announce on reconnect
    if (!relayWs || relayWs.readyState !== WebSocket.OPEN) {
      try {
        await attachTab(tabId, { skipAttachedEvent: true })
        reattachPending.delete(tabId)
        return
      } catch {
        // continue retries
      }
    }

    try {
      await attachTab(tabId)
      reattachPending.delete(tabId)
      return
    } catch {
      // continue retries
    }
  }

  reattachPending.delete(tabId)
  setBadge(tabId, 'off')
  updateGlobalBadge()
}

// --- Tab Lifecycle Listeners ---

chrome.tabs.onCreated.addListener((tab) =>
  void whenReady(() => {
    if (tab.id && tab.url && !isSkippableUrl(tab.url)) {
      void autoAttachTab(tab.id, tab.url)
    }
  }),
)

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) =>
  void whenReady(() => {
    if (changeInfo.status === 'loading' && tab.url && !tabs.has(tabId)) {
      void autoAttachTab(tabId, tab.url)
    }
    const existing = tabs.get(tabId)
    if (existing) {
      if (changeInfo.url) existing.url = changeInfo.url
      if (changeInfo.title) existing.title = changeInfo.title
    }
  }),
)

chrome.tabs.onRemoved.addListener((tabId) =>
  void whenReady(() => {
    reattachPending.delete(tabId)
    if (!tabs.has(tabId)) return
    const tab = tabs.get(tabId)
    if (tab?.sessionId) tabBySession.delete(tab.sessionId)
    tabs.delete(tabId)
    for (const [childSessionId, parentTabId] of childSessionToTab.entries()) {
      if (parentTabId === tabId) childSessionToTab.delete(childSessionId)
    }
    if (tab?.sessionId && tab?.targetId) {
      try {
        sendToRelay({
          method: 'forwardCDPEvent',
          params: {
            method: 'Target.detachedFromTarget',
            params: { sessionId: tab.sessionId, targetId: tab.targetId, reason: 'tab_closed' },
          },
        })
      } catch {
        // Relay may be down
      }
    }
    void persistState()
  }),
)

chrome.tabs.onReplaced.addListener((addedTabId, removedTabId) =>
  void whenReady(() => {
    const tab = tabs.get(removedTabId)
    if (!tab) return
    tabs.delete(removedTabId)
    tabs.set(addedTabId, tab)
    if (tab.sessionId) {
      tabBySession.set(tab.sessionId, addedTabId)
    }
    for (const [childSessionId, parentTabId] of childSessionToTab.entries()) {
      if (parentTabId === removedTabId) {
        childSessionToTab.set(childSessionId, addedTabId)
      }
    }
    setBadge(addedTabId, 'on')
    void persistState()
  }),
)

// --- Debugger Listeners ---

chrome.debugger.onEvent.addListener((...args) => void whenReady(() => onDebuggerEvent(...args)))
chrome.debugger.onDetach.addListener((...args) => void whenReady(() => onDebuggerDetach(...args)))

// --- Action Click ---

chrome.action.onClicked.addListener(() => void whenReady(() => onActionClicked()))

// --- Navigation Badge Refresh ---

chrome.webNavigation.onCompleted.addListener(({ tabId, frameId }) =>
  void whenReady(() => {
    if (frameId !== 0) return
    const tab = tabs.get(tabId)
    if (tab?.state === 'connected') {
      setBadge(tabId, relayWs && relayWs.readyState === WebSocket.OPEN ? 'on' : 'connecting')
    }
  }),
)

chrome.tabs.onActivated.addListener(({ tabId }) =>
  void whenReady(() => {
    const tab = tabs.get(tabId)
    if (tab?.state === 'connected') {
      setBadge(tabId, relayWs && relayWs.readyState === WebSocket.OPEN ? 'on' : 'connecting')
    }
    updateGlobalBadge()
  }),
)

// --- Download Auto-Accept ---

chrome.downloads.onDeterminingFilename.addListener((item, suggest) => {
  suggest({ filename: item.filename, conflictAction: 'uniquify' })
})

// --- Install ---

chrome.runtime.onInstalled.addListener(() => {
  void chrome.runtime.openOptionsPage()
})

// --- Keepalive Alarm ---

chrome.alarms.create('relay-keepalive', { periodInMinutes: 0.4 })

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name !== 'relay-keepalive') return
  await initPromise

  updateGlobalBadge()

  if (await isAutoAttachEnabled()) {
    const allTabs = await chrome.tabs.query({})
    for (const tab of allTabs) {
      if (tab.id && !tabs.has(tab.id) && !isSkippableUrl(tab.url)) {
        void autoAttachTab(tab.id, tab.url)
      }
    }
  }

  if (!relayWs || relayWs.readyState !== WebSocket.OPEN) {
    if (!relayConnectPromise && !reconnectTimer) {
      console.log('Keepalive: WebSocket unhealthy, triggering reconnect')
      await ensureRelayConnection().catch(() => {
        if (!reconnectTimer) scheduleReconnect()
      })
    }
  }
})

// --- Message Handler (options page + status) ---

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type === 'getStatus') {
    sendResponse(handleGetStatus())
    return false
  }

  if (msg?.type === 'relayCheck') {
    const { url, token } = msg
    const headers = token ? { 'x-openclaw-relay-token': token } : {}
    fetch(url, { method: 'GET', headers, signal: AbortSignal.timeout(2000) })
      .then(async (res) => {
        const contentType = String(res.headers.get('content-type') || '')
        let json = null
        if (contentType.includes('application/json')) {
          try {
            json = await res.json()
          } catch {
            json = null
          }
        }
        sendResponse({ status: res.status, ok: res.ok, contentType, json })
      })
      .catch((err) => sendResponse({ status: 0, ok: false, error: String(err) }))
    return true
  }

  return false
})

// --- Initialization ---

const initPromise = rehydrateState()

initPromise.then(async () => {
  try {
    await ensureRelayConnection()
    reconnectAttempt = 0
    if (tabs.size > 0) {
      await reannounceAttachedTabs()
    }
    await autoAttachAllTabs()
  } catch {
    scheduleReconnect()
    if (await isAutoAttachEnabled()) {
      try {
        await autoAttachAllTabs()
      } catch {
        // Best effort
      }
    }
  }
})

async function whenReady(fn) {
  await initPromise
  return fn()
}
'''

def write_background_js(ext_dir):
    path = ext_dir / "background.js"
    path.write_text(BACKGROUND_JS.lstrip('\n'))
    print("  background.js: replaced with auto-attach version")


# ---------------------------------------------------------------------------
# 4. Replace options.js
# ---------------------------------------------------------------------------

OPTIONS_JS = r'''import { deriveRelayToken } from './background-utils.js'
import { classifyRelayCheckException, classifyRelayCheckResponse } from './options-validation.js'

const DEFAULT_PORT = 18792

function clampPort(value) {
  const n = Number.parseInt(String(value || ''), 10)
  if (!Number.isFinite(n)) return DEFAULT_PORT
  if (n <= 0 || n > 65535) return DEFAULT_PORT
  return n
}

function updateRelayUrl(port) {
  const el = document.getElementById('relay-url')
  if (!el) return
  el.textContent = `http://127.0.0.1:${port}/`
}

function setStatus(kind, message) {
  const status = document.getElementById('status')
  if (!status) return
  status.dataset.kind = kind || ''
  status.textContent = message || ''
}

async function checkRelayReachable(port, token) {
  const url = `http://127.0.0.1:${port}/json/version`
  const trimmedToken = String(token || '').trim()
  if (!trimmedToken) {
    setStatus('error', 'Gateway token required. Save your gateway token to connect.')
    return
  }
  try {
    const relayToken = await deriveRelayToken(trimmedToken, port)
    const res = await chrome.runtime.sendMessage({
      type: 'relayCheck',
      url,
      token: relayToken,
    })
    const result = classifyRelayCheckResponse(res, port)
    if (result.action === 'throw') throw new Error(result.error)
    setStatus(result.kind, result.message)
  } catch (err) {
    const result = classifyRelayCheckException(err, port)
    setStatus(result.kind, result.message)
  }
}

async function updateStatusPanel() {
  try {
    const res = await chrome.runtime.sendMessage({ type: 'getStatus' })
    if (!res) return

    const wsDot = document.getElementById('ws-dot')
    const wsStatus = document.getElementById('ws-status')
    const tabsDot = document.getElementById('tabs-dot')
    const tabsStatus = document.getElementById('tabs-status')

    if (wsDot && wsStatus) {
      wsDot.className = 'status-dot'
      if (res.wsState === 'connected') {
        wsDot.classList.add('green')
        wsStatus.textContent = 'WebSocket: connected'
      } else if (res.wsState === 'connecting') {
        wsDot.classList.add('yellow')
        wsStatus.textContent = 'WebSocket: connecting...'
      } else {
        wsDot.classList.add('red')
        wsStatus.textContent = 'WebSocket: disconnected'
      }
    }

    if (tabsDot && tabsStatus) {
      tabsDot.className = 'status-dot'
      if (res.attachedCount > 0) {
        tabsDot.classList.add('green')
        tabsStatus.textContent = `Tabs: ${res.attachedCount} attached`
      } else {
        tabsDot.classList.add('yellow')
        tabsStatus.textContent = 'Tabs: none attached'
      }
    }
  } catch {
    // Extension context may be invalidated
  }
}

async function load() {
  const stored = await chrome.storage.local.get([
    'relayPort',
    'gatewayToken',
    'autoAttach',
    'downloadDirectory',
  ])
  const port = clampPort(stored.relayPort)
  const token = String(stored.gatewayToken || '').trim()
  const autoAttach = stored.autoAttach !== false
  const downloadDir = String(stored.downloadDirectory || '')

  document.getElementById('port').value = String(port)
  document.getElementById('token').value = token
  document.getElementById('auto-attach').checked = autoAttach
  document.getElementById('download-dir').value = downloadDir

  updateRelayUrl(port)
  await checkRelayReachable(port, token)
  await updateStatusPanel()
}

async function save() {
  const portInput = document.getElementById('port')
  const tokenInput = document.getElementById('token')
  const autoAttachInput = document.getElementById('auto-attach')
  const downloadDirInput = document.getElementById('download-dir')

  const port = clampPort(portInput.value)
  const token = String(tokenInput.value || '').trim()
  const autoAttach = autoAttachInput.checked
  const downloadDir = String(downloadDirInput.value || '').trim()

  await chrome.storage.local.set({
    relayPort: port,
    gatewayToken: token,
    autoAttach,
    downloadDirectory: downloadDir,
  })

  portInput.value = String(port)
  tokenInput.value = token
  updateRelayUrl(port)
  await checkRelayReachable(port, token)
}

document.getElementById('save').addEventListener('click', () => void save())
document.getElementById('auto-attach').addEventListener('change', () => void save())

void load()
setInterval(() => void updateStatusPanel(), 3000)
'''

def write_options_js(ext_dir):
    path = ext_dir / "options.js"
    path.write_text(OPTIONS_JS.lstrip('\n'))
    print("  options.js: replaced with auto-attach version")


# ---------------------------------------------------------------------------
# 5. Replace options.html
# ---------------------------------------------------------------------------

OPTIONS_HTML = r'''<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>OpenClaw Browser Relay (Auto-Attach)</title>
    <style>
      :root {
        color-scheme: light dark;
        --accent: #ff5a36;
        --panel: color-mix(in oklab, canvas 92%, canvasText 8%);
        --border: color-mix(in oklab, canvasText 18%, transparent);
        --muted: color-mix(in oklab, canvasText 70%, transparent);
        --shadow: 0 10px 30px color-mix(in oklab, canvasText 18%, transparent);
        font-family: ui-rounded, system-ui, -apple-system, BlinkMacSystemFont, "SF Pro Rounded",
          "SF Pro Display", "Segoe UI", sans-serif;
        line-height: 1.4;
      }
      body {
        margin: 0;
        min-height: 100vh;
        background:
          radial-gradient(1000px 500px at 10% 0%, color-mix(in oklab, var(--accent) 30%, transparent), transparent 70%),
          radial-gradient(900px 450px at 90% 0%, color-mix(in oklab, var(--accent) 18%, transparent), transparent 75%),
          canvas;
        color: canvasText;
      }
      .wrap {
        max-width: 820px;
        margin: 36px auto;
        padding: 0 24px 48px 24px;
      }
      header {
        display: flex;
        align-items: center;
        gap: 12px;
        margin-bottom: 18px;
      }
      .logo {
        width: 44px;
        height: 44px;
        border-radius: 14px;
        background: color-mix(in oklab, var(--accent) 18%, transparent);
        border: 1px solid color-mix(in oklab, var(--accent) 35%, transparent);
        box-shadow: var(--shadow);
        display: grid;
        place-items: center;
      }
      .logo img {
        width: 28px;
        height: 28px;
        image-rendering: pixelated;
      }
      h1 {
        font-size: 20px;
        margin: 0;
        letter-spacing: -0.01em;
      }
      .subtitle {
        margin: 2px 0 0 0;
        color: var(--muted);
        font-size: 13px;
      }
      .grid {
        display: grid;
        grid-template-columns: 1fr;
        gap: 14px;
      }
      .card {
        background: var(--panel);
        border: 1px solid var(--border);
        border-radius: 16px;
        padding: 16px;
        box-shadow: var(--shadow);
      }
      .card h2 {
        margin: 0 0 10px 0;
        font-size: 14px;
        letter-spacing: 0.01em;
      }
      .card p {
        margin: 8px 0 0 0;
        color: var(--muted);
        font-size: 13px;
      }
      .row {
        display: flex;
        align-items: center;
        gap: 8px;
        flex-wrap: wrap;
      }
      label {
        display: block;
        font-size: 12px;
        color: var(--muted);
        margin-bottom: 6px;
      }
      input[type="text"], input[type="password"], input[type="number"], input:not([type]) {
        width: 160px;
        padding: 10px 12px;
        border-radius: 12px;
        border: 1px solid var(--border);
        background: color-mix(in oklab, canvas 92%, canvasText 8%);
        color: canvasText;
        outline: none;
      }
      input:focus {
        border-color: color-mix(in oklab, var(--accent) 70%, transparent);
        box-shadow: 0 0 0 4px color-mix(in oklab, var(--accent) 20%, transparent);
      }
      button {
        padding: 10px 14px;
        border-radius: 12px;
        border: 1px solid color-mix(in oklab, var(--accent) 55%, transparent);
        background: linear-gradient(
          180deg,
          color-mix(in oklab, var(--accent) 80%, white 20%),
          var(--accent)
        );
        color: white;
        font-weight: 650;
        letter-spacing: 0.01em;
        cursor: pointer;
      }
      button:active {
        transform: translateY(1px);
      }
      .hint {
        margin-top: 10px;
        font-size: 12px;
        color: var(--muted);
      }
      code {
        font-family: ui-monospace, Menlo, Monaco, Consolas, "SF Mono", monospace;
        font-size: 12px;
      }
      a {
        color: color-mix(in oklab, var(--accent) 85%, canvasText 15%);
      }
      .status {
        margin-top: 10px;
        font-size: 12px;
        color: color-mix(in oklab, var(--accent) 70%, canvasText 30%);
        min-height: 16px;
      }
      .status[data-kind='ok'] {
        color: color-mix(in oklab, #16a34a 75%, canvasText 25%);
      }
      .status[data-kind='error'] {
        color: color-mix(in oklab, #ef4444 75%, canvasText 25%);
      }
      .status-row {
        display: flex;
        align-items: center;
        gap: 8px;
        margin: 6px 0;
        font-size: 13px;
      }
      .status-dot {
        width: 8px;
        height: 8px;
        border-radius: 50%;
        background: #888;
        flex-shrink: 0;
      }
      .status-dot.green { background: #16a34a; }
      .status-dot.yellow { background: #F59E0B; }
      .status-dot.red { background: #B91C1C; }
      .toggle-row {
        display: flex;
        align-items: center;
        gap: 10px;
        margin: 8px 0;
      }
      .toggle-row label {
        margin: 0;
        font-size: 13px;
        color: canvasText;
      }
    </style>
  </head>
  <body>
    <div class="wrap">
      <header>
        <div class="logo" aria-hidden="true">
          <img src="icons/icon128.png" alt="" />
        </div>
        <div>
          <h1>OpenClaw Browser Relay</h1>
          <p class="subtitle">Auto-attaches debugger to all tabs. Click toolbar icon to toggle.</p>
        </div>
      </header>

      <div class="grid">
        <div class="card">
          <h2>Status</h2>
          <div class="status-row">
            <div id="ws-dot" class="status-dot"></div>
            <span id="ws-status">WebSocket: checking...</span>
          </div>
          <div class="status-row">
            <div id="tabs-dot" class="status-dot"></div>
            <span id="tabs-status">Tabs: checking...</span>
          </div>
        </div>

        <div class="card">
          <h2>Getting started</h2>
          <p>
            If you see a red badge on the extension icon, the relay server is not reachable.
            Start OpenClaw's browser relay on this machine, then click the toolbar button.
          </p>
          <p>
            Full guide: <a href="https://docs.openclaw.ai/tools/chrome-extension" target="_blank" rel="noreferrer">docs.openclaw.ai/tools/chrome-extension</a>
          </p>
        </div>

        <div class="card">
          <h2>Relay connection</h2>
          <label for="port">Port</label>
          <div class="row">
            <input id="port" inputmode="numeric" pattern="[0-9]*" />
          </div>
          <label for="token" style="margin-top: 10px">Gateway token</label>
          <div class="row">
            <input id="token" type="password" autocomplete="off" style="width: min(520px, 100%)" />
            <button id="save" type="button">Save</button>
          </div>
          <div class="hint">
            Default port: <code>18792</code>. Extension connects to: <code id="relay-url">http://127.0.0.1:&lt;port&gt;/</code>.
            Gateway token must match <code>gateway.auth.token</code> (or <code>OPENCLAW_GATEWAY_TOKEN</code>).
          </div>
          <div class="status" id="status"></div>
        </div>

        <div class="card">
          <h2>Settings</h2>
          <div class="toggle-row">
            <input type="checkbox" id="auto-attach" checked />
            <label for="auto-attach">Auto-attach to all tabs</label>
          </div>
          <label for="download-dir" style="margin-top: 10px">Download directory (optional)</label>
          <div class="row">
            <input id="download-dir" type="text" placeholder="Default downloads folder" style="width: min(520px, 100%)" />
          </div>
        </div>
      </div>

      <script type="module" src="options.js"></script>
    </div>
  </body>
</html>
'''

def write_options_html(ext_dir):
    path = ext_dir / "options.html"
    path.write_text(OPTIONS_HTML.lstrip('\n'))
    print("  options.html: replaced with auto-attach version")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    argv_path = sys.argv[1] if len(sys.argv) > 1 else None
    ext_dir = find_extension_dir(argv_path)

    manifest_path = ext_dir / "manifest.json"
    with open(manifest_path) as f:
        m = json.load(f)
    if "OpenClaw" not in m.get("name", ""):
        print(f"Error: {manifest_path} does not appear to be an OpenClaw extension")
        sys.exit(1)

    print(f"Patching extension at: {ext_dir}")
    print()

    backup(ext_dir)
    print()

    print("Applying patches:")
    patch_manifest(ext_dir)
    patch_background_utils(ext_dir)
    write_background_js(ext_dir)
    write_options_js(ext_dir)
    write_options_html(ext_dir)

    print()
    print("Done! Reload the extension in chrome://extensions/ to apply changes.")
    print()
    print("What changed:")
    print("  - Auto-attaches debugger to ALL open tabs (no clicking needed)")
    print("  - New permissions: cookies, downloads, <all_urls>")
    print("  - Custom commands: Tab.list/attachAll/getStatus, Cookie.*, Download.*")
    print("  - Status panel and auto-attach toggle in options page")
    print("  - Global badge: green+count / yellow / red")
    print("  - Keepalive every ~24s to prevent MV3 service worker termination")
    print()
    print("To revert: copy files from .backup-before-autoattach/ back to the extension dir")


if __name__ == '__main__':
    main()
