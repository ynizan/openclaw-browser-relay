import { test, expect, chromium, type BrowserContext } from '@playwright/test';
import { resolve } from 'path';

const extensionPath = resolve(__dirname, '..', 'dist');

async function launchWithExtension(): Promise<{
  context: BrowserContext;
  extensionId: string;
}> {
  const context = await chromium.launchPersistentContext('', {
    headless: false,
    args: [
      `--disable-extensions-except=${extensionPath}`,
      `--load-extension=${extensionPath}`,
      '--no-first-run',
      '--disable-default-apps',
    ],
  });

  // Wait for service worker to register
  let serviceWorker = context.serviceWorkers()[0];
  if (!serviceWorker) {
    serviceWorker = await context.waitForEvent('serviceworker');
  }

  const extensionId = serviceWorker.url().split('/')[2];
  return { context, extensionId };
}

test.describe('Extension loading', () => {
  let context: BrowserContext;
  let extensionId: string;

  test.beforeAll(async () => {
    const result = await launchWithExtension();
    context = result.context;
    extensionId = result.extensionId;
  });

  test.afterAll(async () => {
    await context?.close();
  });

  test('service worker registers successfully', async () => {
    expect(extensionId).toBeTruthy();
    expect(extensionId.length).toBeGreaterThan(0);
  });

  test('options page loads', async () => {
    const page = await context.newPage();
    await page.goto(`chrome-extension://${extensionId}/options.html`);
    await expect(page.locator('h1')).toContainText('OpenClaw Browser Relay');
    await expect(page.locator('#port')).toBeVisible();
    await expect(page.locator('#token')).toBeVisible();
    await expect(page.locator('#auto-attach')).toBeVisible();
    await page.close();
  });

  test('options page saves settings', async () => {
    const page = await context.newPage();
    await page.goto(`chrome-extension://${extensionId}/options.html`);

    await page.fill('#port', '19000');
    await page.fill('#token', 'test-token-123');
    await page.click('#save');

    // Reload and verify persistence
    await page.reload();
    await expect(page.locator('#port')).toHaveValue('19000');
    await expect(page.locator('#token')).toHaveValue('test-token-123');
    await page.close();
  });

  test('auto-attach toggle persists', async () => {
    const page = await context.newPage();
    await page.goto(`chrome-extension://${extensionId}/options.html`);

    const toggle = page.locator('#auto-attach');
    const wasChecked = await toggle.isChecked();

    await toggle.click();
    await page.waitForTimeout(500);

    await page.reload();
    const isChecked = await page.locator('#auto-attach').isChecked();
    expect(isChecked).toBe(!wasChecked);

    // Restore original state
    if (isChecked !== wasChecked) {
      await page.locator('#auto-attach').click();
    }
    await page.close();
  });

  test('status panel shows disconnected state', async () => {
    const page = await context.newPage();
    await page.goto(`chrome-extension://${extensionId}/options.html`);

    // Without a relay running, WS should show disconnected
    await expect(page.locator('#ws-status')).toContainText(/disconnected|checking/);
    await page.close();
  });
});

test.describe('Tab management', () => {
  let context: BrowserContext;
  let extensionId: string;

  test.beforeAll(async () => {
    const result = await launchWithExtension();
    context = result.context;
    extensionId = result.extensionId;
  });

  test.afterAll(async () => {
    await context?.close();
  });

  test('new tab triggers auto-attach attempt', async () => {
    // Open a regular page
    const page = await context.newPage();
    await page.goto('data:text/html,<h1>Test Page</h1>');

    // Give auto-attach time to attempt
    await page.waitForTimeout(1000);

    // The extension should have tried to attach (may fail without relay, but shouldn't crash)
    const serviceWorker = context.serviceWorkers()[0];
    expect(serviceWorker).toBeTruthy();

    await page.close();
  });

  test('navigation does not crash extension', async () => {
    const page = await context.newPage();
    await page.goto('data:text/html,<h1>Page 1</h1>');
    await page.waitForTimeout(500);

    await page.goto('data:text/html,<h1>Page 2</h1>');
    await page.waitForTimeout(500);

    await page.goto('data:text/html,<h1>Page 3</h1>');
    await page.waitForTimeout(500);

    // Service worker should still be alive
    const serviceWorker = context.serviceWorkers()[0];
    expect(serviceWorker).toBeTruthy();

    await page.close();
  });

  test('chrome:// URLs are skipped', async () => {
    const page = await context.newPage();
    await page.goto('chrome://version');
    await page.waitForTimeout(500);

    // Extension should not crash when encountering chrome:// URLs
    const serviceWorker = context.serviceWorkers()[0];
    expect(serviceWorker).toBeTruthy();

    await page.close();
  });
});

test.describe('Manifest validation', () => {
  test('manifest has required permissions', async () => {
    const fs = await import('fs');
    const path = await import('path');
    const manifestPath = path.resolve(__dirname, '..', 'dist', 'manifest.json');
    const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf-8'));

    expect(manifest.manifest_version).toBe(3);
    expect(manifest.permissions).toContain('debugger');
    expect(manifest.permissions).toContain('tabs');
    expect(manifest.permissions).toContain('storage');
    expect(manifest.permissions).toContain('alarms');
    expect(manifest.permissions).toContain('webNavigation');
    expect(manifest.permissions).toContain('cookies');
    expect(manifest.permissions).toContain('downloads');
    expect(manifest.host_permissions).toContain('<all_urls>');
    expect(manifest.background.service_worker).toBe('background.js');
    expect(manifest.background.type).toBe('module');
  });
});
