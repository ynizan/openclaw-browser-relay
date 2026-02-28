# OpenClaw Browser Relay — Auto-Attach Patch

Patches the official OpenClaw browser relay extension to **auto-attach the Chrome debugger to all tabs** — no manual clicking required.

```
OpenClaw Gateway <-> CDP Relay (:18792) <-WS-> Extension <-chrome.debugger-> All Tabs
```

## Quick Start

### 1. Install the official extension

```bash
npm install -g openclaw@latest
openclaw browser extension install
```

### 2. Get the extension path

```bash
openclaw browser extension path
# e.g. ~/.openclaw/browser/chrome-extension/
```

### 3. Load in Chrome (unpacked)

1. `chrome://extensions/` -> enable **Developer mode**
2. **Load unpacked** -> select the path from step 2
3. Set your **gateway token** in the extension options page
4. Verify the relay is reachable (green status)

### 4. Apply the auto-attach patch

```bash
python3 patch_auto_attach.py
```

Or specify the path explicitly:

```bash
python3 patch_auto_attach.py ~/.openclaw/browser/chrome-extension/
```

### 5. Reload the extension

Go to `chrome://extensions/` and click the reload button on the extension.

Chrome will show a "This extension can debug your browser" warning — expected for CDP access.

## What the Patch Changes

| Feature | Official | Patched |
|---------|----------|---------|
| Tab attachment | Manual click per tab | Auto-attach all tabs |
| Host permissions | Per-site | `<all_urls>` |
| Extra permissions | — | `cookies`, `downloads` |
| Keepalive interval | 30s | 24s |
| Custom commands | — | Tab, Cookie, Download APIs |
| Options page | Port + token | + status panel, auto-attach toggle, download dir |
| Badge | ON/OFF per tab | Green+count / Yellow / Red globally |

The patch preserves the original's gateway handshake protocol, reconnect logic, and state persistence.

## Reverting

The script backs up all original files before patching:

```bash
cp ~/.openclaw/browser/chrome-extension/.backup-before-autoattach/* \
   ~/.openclaw/browser/chrome-extension/
```

Or just reinstall:

```bash
openclaw browser extension install
```

## Configure

The extension opens its options page on first install.

| Setting | Default | Description |
|---------|---------|-------------|
| **Relay port** | `18792` | WebSocket relay port (typically gateway port + 3) |
| **Gateway token** | — | Must match `OPENCLAW_GATEWAY_TOKEN` env var or `gateway.auth.token` |
| **Auto-attach** | ON | Automatically attach debugger to all tabs |
| **Download directory** | — | Optional default download path |

### Token Derivation

The extension derives a relay-specific token (never sends the gateway token directly):

```
relayToken = HMAC-SHA256(gatewayToken, "openclaw-extension-relay-v1:<port>")
```

## How It Works

1. On startup, connects via WebSocket to `ws://127.0.0.1:<port>/extension?token=<derived>`
2. Completes gateway handshake (connect.challenge / connect protocol v3)
3. Auto-attaches Chrome debugger to all open tabs
4. New tabs are automatically attached as they open
5. On navigation, re-attaches automatically (3 retries: 300ms, 700ms, 1500ms)
6. Keepalive alarm every ~24s prevents MV3 service worker termination

## Badge Indicators

| Badge | Meaning |
|-------|---------|
| Green + number | Connected to relay, N tabs attached |
| Yellow | Connected, no tabs attached |
| Red | Disconnected from relay |

Click the extension icon to toggle attach/detach all tabs.

## Custom Commands

Beyond standard CDP, the patched extension supports:

### Tab Management
- `Tab.list` — list all attached tabs with session IDs
- `Tab.attachAll` — force attach to all open tabs
- `Tab.getStatus` — extension status (WS state, attached count, uptime)

### Cookies
- `Cookie.getAll` — get cookies (filter by domain, url, name)
- `Cookie.set` / `Cookie.remove`
- `Cookie.export` / `Cookie.import`

### Downloads
- `Download.start` / `Download.list` / `Download.getStatus`
- `Download.cancel` / `Download.open`

## Troubleshooting

**Red badge (disconnected)**
- Verify the CDP relay is running: `curl http://127.0.0.1:18792/json/version`
- Check gateway token matches `OPENCLAW_GATEWAY_TOKEN`
- Inspect service worker console: `chrome://extensions/` -> Inspect views

**"Debugger detached" warnings**
- Normal during navigation — auto-reattaches within ~1.5s

**Service worker stopped**
- Keepalive alarm restores connection within 24s
- State persisted via `chrome.storage.session`

**Extension not attaching to a tab**
- `chrome://`, `chrome-extension://`, `about:`, `devtools://` URLs are skipped
- The extension's own options page is excluded

## Development (fork)

This repo also contains a TypeScript fork with build tooling:

```bash
npm install
npm run build     # build to dist/
npm run watch     # dev mode with source maps
npm run lint      # TypeScript type checking
npm run test      # Playwright tests
npm run package   # build + zip
```
