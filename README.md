# OpenClaw Browser Relay (Auto-Attach)

A fork of the official OpenClaw browser relay extension that **auto-attaches the Chrome debugger to all tabs** — no manual clicking required.

```
OpenClaw Gateway <-> CDP Relay (:18792) <-WS-> Extension <-chrome.debugger-> All Tabs
```

## Prerequisites

- Node.js 20+
- Chrome or Chromium browser
- Running OpenClaw gateway with browser relay enabled (`OPENCLAW_GATEWAY_TOKEN` set)

## Build from Source

```bash
git clone https://github.com/ynizan/openclaw-browser-relay.git
cd openclaw-browser-relay
npm install
npm run build
```

The built extension will be in the `dist/` directory.

## Install in Chrome

1. Navigate to `chrome://extensions/`
2. Enable **Developer mode** (top right toggle)
3. Click **Load unpacked** and select the `dist/` directory
4. Chrome will show a "This extension can debug your browser" warning — this is expected, as CDP access requires the `debugger` permission

## Configure

The extension opens its options page automatically on first install.

| Setting | Default | Description |
|---------|---------|-------------|
| **Relay port** | `18792` | WebSocket relay port (typically gateway port + 3) |
| **Gateway token** | — | Must match `OPENCLAW_GATEWAY_TOKEN` env var or `gateway.auth.token` in your gateway config |
| **Auto-attach** | ON | Automatically attach debugger to all tabs |
| **Download directory** | — | Optional default download path |

### Token Derivation

The extension doesn't send your gateway token directly. Instead, it derives a relay-specific token using HMAC-SHA256:

```
relayToken = HMAC-SHA256(gatewayToken, "openclaw-extension-relay-v1:<port>")
```

## How It Works

1. On startup, the extension connects via WebSocket to `ws://127.0.0.1:<port>/extension?token=<derived>`
2. It auto-attaches the Chrome debugger to all open tabs
3. OpenClaw sends CDP commands through the relay, and the extension executes them via `chrome.debugger`
4. New tabs are automatically attached as they open
5. On navigation, the debugger re-attaches automatically (3 retries: 300ms, 700ms, 1500ms)
6. A keepalive alarm fires every ~24s to prevent MV3 service worker termination

## Badge Indicators

| Badge | Meaning |
|-------|---------|
| Green + number | Connected to relay, N tabs attached |
| Yellow | Connected to relay, no tabs attached |
| Red | Disconnected from relay |

Click the extension icon to toggle attach/detach all tabs.

## Custom Commands

Beyond standard CDP protocol commands, the extension supports:

### Tab Management
- `Tab.list` — list all attached tabs with session IDs
- `Tab.attachAll` — force attach to all open tabs
- `Tab.getStatus` — get extension status (WS state, attached count, uptime)

### Cookies
- `Cookie.getAll` — get cookies (filter by domain, url, name)
- `Cookie.set` — set a cookie
- `Cookie.remove` — remove a cookie
- `Cookie.export` — export cookies for a domain/url
- `Cookie.import` — import an array of cookies

### Downloads
- `Download.start` — start a download
- `Download.list` — list recent downloads
- `Download.getStatus` — get status of a download
- `Download.cancel` — cancel an active download
- `Download.open` — open a completed download

## Differences from Official Extension

| Feature | Official | This Fork |
|---------|----------|-----------|
| Tab attachment | Manual click per tab | Auto-attach all tabs |
| Host permissions | Per-site | `<all_urls>` |
| Extra permissions | — | `cookies`, `downloads` |
| Reconnect | Basic | Exponential backoff with jitter (1s–30s) |
| Service worker survival | — | Keepalive alarm every 24s |
| State persistence | — | Survives service worker restarts via `chrome.storage.session` |
| Custom commands | — | Tab, Cookie, Download APIs |

## Development

```bash
npm run watch      # dev build with file watching and source maps
npm run lint       # TypeScript type checking (tsc --noEmit)
npm run test       # Playwright tests
npm run package    # build + zip for distribution
```

## Troubleshooting

**Red badge (disconnected)**
- Verify the CDP relay is running on the configured port
- Check that the gateway token matches your `OPENCLAW_GATEWAY_TOKEN`
- Look at the service worker console (`chrome://extensions/` -> Inspect views) for error details

**"Debugger detached" warnings**
- Normal during page navigation — the extension automatically re-attaches within ~1.5s

**Service worker stopped**
- The keepalive alarm restores the connection within 24 seconds
- State is persisted via `chrome.storage.session`, so attached tabs are rehydrated on restart

**Extension not attaching to a tab**
- `chrome://`, `chrome-extension://`, `about:`, and `devtools://` URLs are intentionally skipped
- The extension's own options page is also excluded
