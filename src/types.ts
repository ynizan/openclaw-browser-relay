export interface TabSession {
  state: 'connecting' | 'connected';
  sessionId?: string;
  targetId?: string;
  attachOrder?: number;
  url?: string;
  title?: string;
  attachedAt?: number;
}

export interface PersistedTab {
  tabId: number;
  sessionId: string;
  targetId: string;
  attachOrder: number;
}

export interface BadgeConfig {
  text: string;
  color: string;
}

export type BadgeKind = 'on' | 'off' | 'connecting' | 'error';

export interface RelayMessage {
  id?: number;
  method?: string;
  params?: Record<string, unknown>;
  result?: unknown;
  error?: string;
}

export interface ForwardCDPCommandMessage {
  id: number;
  method: 'forwardCDPCommand';
  params: {
    method: string;
    params?: Record<string, unknown>;
    sessionId?: string;
  };
}

export interface ForwardCDPEventPayload {
  method: 'forwardCDPEvent';
  params: {
    method: string;
    sessionId?: string;
    params?: Record<string, unknown>;
  };
}

export interface TabListEntry {
  tabId: number;
  sessionId: string;
  targetId: string;
  url: string;
  title: string;
  status: string;
  attachedAt: number;
}

export interface ExtensionStatus {
  wsState: 'connected' | 'connecting' | 'disconnected';
  attachedCount: number;
  tabs: TabListEntry[];
  uptime: number;
}

export interface CookieSpec {
  url?: string;
  domain?: string;
  name?: string;
  value?: string;
  path?: string;
  secure?: boolean;
  httpOnly?: boolean;
  sameSite?: chrome.cookies.SameSiteStatus;
  expirationDate?: number;
}

export interface DownloadOptions {
  url: string;
  filename?: string;
  saveAs?: boolean;
}

export interface ReconnectOptions {
  baseMs?: number;
  maxMs?: number;
  jitterMs?: number;
  random?: () => number;
}

export interface RelayCheckRequest {
  type: 'relayCheck';
  url: string;
  token: string;
}

export interface RelayCheckResponse {
  status: number;
  ok: boolean;
  contentType?: string;
  json?: unknown;
  error?: string;
}

export interface ExtensionSettings {
  relayPort: number;
  gatewayToken: string;
  downloadDirectory: string;
  autoAttach: boolean;
}
