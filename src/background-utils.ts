import type { ReconnectOptions } from './types.js';

export function reconnectDelayMs(
  attempt: number,
  opts: ReconnectOptions = {},
): number {
  const baseMs = Number.isFinite(opts.baseMs) ? opts.baseMs! : 1000;
  const maxMs = Number.isFinite(opts.maxMs) ? opts.maxMs! : 30000;
  const jitterMs = Number.isFinite(opts.jitterMs) ? opts.jitterMs! : 1000;
  const random = typeof opts.random === 'function' ? opts.random : Math.random;
  const safeAttempt = Math.max(0, Number.isFinite(attempt) ? attempt : 0);
  const backoff = Math.min(baseMs * 2 ** safeAttempt, maxMs);
  return backoff + Math.max(0, jitterMs) * random();
}

export async function deriveRelayToken(gatewayToken: string, port: number): Promise<string> {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    'raw',
    enc.encode(gatewayToken),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign'],
  );
  const sig = await crypto.subtle.sign(
    'HMAC',
    key,
    enc.encode(`openclaw-extension-relay-v1:${port}`),
  );
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

export async function buildRelayWsUrl(port: number, gatewayToken: string): Promise<string> {
  const token = String(gatewayToken || '').trim();
  if (!token) {
    throw new Error(
      'Missing gatewayToken in extension settings (chrome.storage.local.gatewayToken)',
    );
  }
  const relayToken = await deriveRelayToken(token, port);
  return `ws://127.0.0.1:${port}/extension?token=${encodeURIComponent(relayToken)}`;
}

export function isRetryableReconnectError(err: unknown): boolean {
  const message = err instanceof Error ? err.message : String(err || '');
  if (message.includes('Missing gatewayToken')) {
    return false;
  }
  return true;
}

export function isSkippableUrl(url: string | undefined): boolean {
  if (!url) return true;
  return (
    url.startsWith('chrome://') ||
    url.startsWith('chrome-extension://') ||
    url.startsWith('about:') ||
    url.startsWith('devtools://')
  );
}
