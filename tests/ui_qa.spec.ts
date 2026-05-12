// CoPartner UI/UX 自動QAスペック
// 起動方法: copartner/ 配下で `npm run test:ui`
// 前提: 別ターミナルで uvicorn が起動していること
//       python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

import { test, expect, Page } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';

const BASE = process.env.COPARTNER_BASE || 'http://127.0.0.1:8000';
const TS = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
const OUT_DIR = path.resolve(__dirname, '..', '..', 'deliverables', `qa_uiux_${TS}`);
const SCREENSHOTS_DIR = path.join(OUT_DIR, 'screenshots');

// 出力ディレクトリ準備
test.beforeAll(() => {
  fs.mkdirSync(SCREENSHOTS_DIR, { recursive: true });
});

interface PageReport {
  route: string;
  status: number | null;
  consoleErrors: string[];
  consoleWarnings: string[];
  networkErrors: string[];
  screenshotPath: string;
}

const reports: PageReport[] = [];

async function visitAndCapture(page: Page, route: string): Promise<PageReport> {
  const consoleErrors: string[] = [];
  const consoleWarnings: string[] = [];
  const networkErrors: string[] = [];

  page.on('console', (msg) => {
    const text = `[${msg.type()}] ${msg.text()}`;
    if (msg.type() === 'error') consoleErrors.push(text);
    if (msg.type() === 'warning') consoleWarnings.push(text);
  });
  page.on('response', (resp) => {
    if (resp.status() >= 400) {
      networkErrors.push(`[${resp.status()}] ${resp.url()}`);
    }
  });
  page.on('pageerror', (err) => {
    consoleErrors.push(`[pageerror] ${err.message}`);
  });

  const url = BASE + route;
  const safeName = route.replace(/\//g, '_').replace(/^_/, 'root') || 'root';
  const screenshotPath = path.join(SCREENSHOTS_DIR, `${safeName}.png`);

  let status: number | null = null;
  try {
    const resp = await page.goto(url, { waitUntil: 'networkidle', timeout: 15000 });
    status = resp ? resp.status() : null;
    await page.screenshot({ path: screenshotPath, fullPage: true });
  } catch (err: any) {
    consoleErrors.push(`[navigation] ${err.message}`);
  }

  return {
    route,
    status,
    consoleErrors,
    consoleWarnings,
    networkErrors,
    screenshotPath,
  };
}

test('CoPartner 主要ルート巡回（未認証）', async ({ page }) => {
  // ログイン不要で確認できるルート
  const publicRoutes = ['/', '/login'];

  for (const route of publicRoutes) {
    const report = await visitAndCapture(page, route);
    reports.push(report);
    // ステータスチェック（200または302リダイレクトはOK、500系はNG）
    expect(report.status, `${route} が5xx応答: ${report.status}`).toBeLessThan(500);
  }
});

// TODO: ログインが必要なルート（/dashboard, /clients/*, /analysis/* 等）は
// テスト用アカウントを .env に用意してから有効化する
test.skip('CoPartner 主要ルート巡回（要ログイン）', async ({ page }) => {
  // ログイン処理（テスト用アカウントが必要）
  await page.goto(BASE + '/login');
  await page.fill('input[name="username"]', process.env.TEST_USER || 'testuser');
  await page.fill('input[name="password"]', process.env.TEST_PASS || 'testpass');
  await page.click('button[type="submit"]');

  const authRoutes = ['/dashboard'];
  for (const route of authRoutes) {
    const report = await visitAndCapture(page, route);
    reports.push(report);
    expect(report.status, `${route} が5xx応答: ${report.status}`).toBeLessThan(500);
  }
});

// 全テスト終了後、レポートをファイル出力
test.afterAll(() => {
  const reportPath = path.join(OUT_DIR, 'report.json');
  fs.writeFileSync(reportPath, JSON.stringify(reports, null, 2));

  // human-readable サマリ
  const summaryLines = [
    '# UI/UX QA レポート',
    `日時: ${new Date().toLocaleString('ja-JP')}`,
    `Base URL: ${BASE}`,
    '',
    '## 巡回結果',
    '',
    '| ルート | HTTP | console.error | network err |',
    '|---|---|---|---|',
  ];
  for (const r of reports) {
    summaryLines.push(
      `| \`${r.route}\` | ${r.status ?? 'N/A'} | ${r.consoleErrors.length} | ${r.networkErrors.length} |`
    );
  }
  summaryLines.push('', '## 詳細', '');
  for (const r of reports) {
    if (r.consoleErrors.length === 0 && r.networkErrors.length === 0) continue;
    summaryLines.push(`### \`${r.route}\``);
    if (r.consoleErrors.length > 0) {
      summaryLines.push('**console errors**:');
      r.consoleErrors.forEach((e) => summaryLines.push(`- ${e}`));
    }
    if (r.networkErrors.length > 0) {
      summaryLines.push('**network errors**:');
      r.networkErrors.forEach((e) => summaryLines.push(`- ${e}`));
    }
    summaryLines.push('');
  }
  fs.writeFileSync(path.join(OUT_DIR, 'report.md'), summaryLines.join('\n'));
  console.log(`✅ QA report saved: ${OUT_DIR}`);
});
