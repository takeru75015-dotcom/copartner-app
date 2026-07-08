// CoPartner UI/UX 自動QAスペック
// 起動方法: copartner/ 配下で `npm run test:ui`
// 前提: 別ターミナルで uvicorn が起動していること
//       python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

import { test, expect, Page, BrowserContext } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';

const BASE = process.env.COPARTNER_BASE || 'http://127.0.0.1:8000';
const TEST_USER = process.env.TEST_USER || 'test';
const TEST_PASS = process.env.TEST_PASS || 'test';

const TS = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
const DATE_TAG = TS.slice(0, 10);
const OUT_DIR = path.resolve(__dirname, '..', '..', 'deliverables', `qa_uiux_${DATE_TAG}`);
const SCREENSHOTS_DIR = path.join(OUT_DIR, 'screenshots');

test.beforeAll(() => {
  fs.mkdirSync(SCREENSHOTS_DIR, { recursive: true });
});

interface PageReport {
  label: string;
  route: string;
  status: number | null;
  title: string;
  consoleErrors: string[];
  consoleWarnings: string[];
  networkErrors: string[];
  pageErrors: string[];
  screenshotPath: string;
}

const reports: PageReport[] = [];

function attachListeners(page: Page, r: PageReport) {
  page.on('console', (msg) => {
    const text = `[${msg.type()}] ${msg.text()}`;
    if (msg.type() === 'error') r.consoleErrors.push(text);
    if (msg.type() === 'warning') r.consoleWarnings.push(text);
  });
  page.on('response', (resp) => {
    if (resp.status() >= 400) {
      r.networkErrors.push(`[${resp.status()}] ${resp.request().method()} ${resp.url()}`);
    }
  });
  page.on('pageerror', (err) => {
    r.pageErrors.push(`[pageerror] ${err.message}`);
  });
}

async function visit(page: Page, label: string, route: string): Promise<PageReport> {
  const safeName = (label + '_' + route).replace(/[^a-zA-Z0-9_-]+/g, '_').slice(0, 80);
  const screenshotPath = path.join(SCREENSHOTS_DIR, `${safeName}.png`);
  const r: PageReport = {
    label,
    route,
    status: null,
    title: '',
    consoleErrors: [],
    consoleWarnings: [],
    networkErrors: [],
    pageErrors: [],
    screenshotPath,
  };
  attachListeners(page, r);
  try {
    const resp = await page.goto(BASE + route, { waitUntil: 'networkidle', timeout: 20000 });
    r.status = resp ? resp.status() : null;
    r.title = await page.title().catch(() => '');
    await page.screenshot({ path: screenshotPath, fullPage: true });
  } catch (err: any) {
    r.pageErrors.push(`[navigation] ${err.message}`);
  }
  reports.push(r);
  return r;
}

async function loginAs(page: Page, username: string, password: string) {
  await page.goto(BASE + '/login', { waitUntil: 'networkidle' });
  await page.fill('input[name="username"]', username);
  await page.fill('input[name="password"]', password);
  await Promise.all([
    page.waitForURL(/\/dashboard/, { timeout: 10000 }).catch(() => null),
    page.click('button[type="submit"]'),
  ]);
}

test.describe('CoPartner UI/UX QA', () => {
  test.describe.configure({ mode: 'serial' });

  // ────────────────────────────────────────────────
  // 公開ページ（未認証）
  // ────────────────────────────────────────────────
  test('公開ページ巡回', async ({ browser }) => {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();

    await visit(page, 'root',     '/');
    await visit(page, 'login',    '/login');
    await visit(page, 'register', '/register');

    await ctx.close();
  });

  // ────────────────────────────────────────────────
  // 認証必須ページ（test/test でログイン）
  // ────────────────────────────────────────────────
  test('認証ページ巡回', async ({ browser }) => {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();

    // ログイン
    const loginReport: PageReport = {
      label: 'login_post', route: '/login (POST)', status: null, title: '',
      consoleErrors: [], consoleWarnings: [], networkErrors: [], pageErrors: [],
      screenshotPath: path.join(SCREENSHOTS_DIR, 'login_post.png'),
    };
    attachListeners(page, loginReport);
    await loginAs(page, TEST_USER, TEST_PASS);
    loginReport.title = await page.title().catch(() => '');
    loginReport.status = page.url().includes('/dashboard') ? 200 : 0;
    await page.screenshot({ path: loginReport.screenshotPath, fullPage: true }).catch(() => {});
    reports.push(loginReport);
    expect(page.url(), 'ログイン後に /dashboard に遷移すること').toContain('/dashboard');

    // 認証必須ページ巡回（test userの実データ: client_id=2, fd_id=1）
    const CLIENT_ID = 2;
    const FD_ID = 1;

    await visit(page, 'dashboard',         '/dashboard');
    await visit(page, 'client_detail',     `/clients/${CLIENT_ID}`);
    await visit(page, 'analyze',           `/financials/${FD_ID}/analyze`);
    // A3検証: ids なしで /compare を踏むと client_detail に ?error= 付きでリダイレクトされる
    await visit(page, 'comparison_no_ids', `/clients/${CLIENT_ID}/compare`);
    // 比較ページの正常パス（ids 付き）
    await visit(page, 'comparison_ok',     `/clients/${CLIENT_ID}/compare?ids=1,2`);
    await visit(page, 'preview',           `/financials/${FD_ID}/preview`);
    await visit(page, 'edit_financial',    `/financials/${FD_ID}/edit`);
    await visit(page, 'subsidy_referral',  '/subsidy-referral');
    await visit(page, 'partner_referral',  '/partner-referral');

    await ctx.close();
  });

  // ────────────────────────────────────────────────
  // レポート出力
  // ────────────────────────────────────────────────
  test.afterAll(() => {
    fs.writeFileSync(path.join(OUT_DIR, 'report.json'), JSON.stringify(reports, null, 2));

    const allConsole = reports.flatMap(r => r.consoleErrors.map(e => ({ route: r.route, msg: e })));
    const allPageErr = reports.flatMap(r => r.pageErrors.map(e => ({ route: r.route, msg: e })));
    const allNet     = reports.flatMap(r => r.networkErrors.map(e => ({ route: r.route, msg: e })));
    fs.writeFileSync(path.join(OUT_DIR, 'console_logs.json'), JSON.stringify({ consoleErrors: allConsole, pageErrors: allPageErr }, null, 2));
    fs.writeFileSync(path.join(OUT_DIR, 'network_errors.json'), JSON.stringify(allNet, null, 2));

    const okCount     = reports.filter(r => (r.status ?? 0) >= 200 && (r.status ?? 0) < 400).length;
    const failCount   = reports.filter(r => (r.status ?? 0) >= 500 || r.status === 0 || r.status === null).length;
    const hasJsErr    = reports.some(r => r.consoleErrors.length + r.pageErrors.length > 0);
    const hasNetErr   = reports.some(r => r.networkErrors.length > 0);

    let verdict = '✅ PASS';
    if (failCount > 0 || hasJsErr) verdict = '❌ FAIL';
    else if (hasNetErr) verdict = '⚠️ 要確認';

    const lines: string[] = [];
    lines.push('# UI/UX QA レポート');
    lines.push(`日時: ${new Date().toLocaleString('ja-JP')}`);
    lines.push(`Base URL: ${BASE}`);
    lines.push(`対象: copartner/app (FastAPI + Jinja2)`);
    lines.push(`テストアカウント: ${TEST_USER}`);
    lines.push('');
    lines.push(`## 判定: ${verdict}`);
    lines.push('');
    lines.push('### 判定基準');
    lines.push('- ✅ PASS: 全ルートが200/300応答、console.error/pageerror 0件、network 4xx/5xx 0件');
    lines.push('- ⚠️ 要確認: network 4xx/5xx あり（軽微）、JSエラーなし');
    lines.push('- ❌ FAIL: console.error or pageerror あり、5xx応答、または主要遷移失敗');
    lines.push('');
    lines.push('## 巡回結果サマリ');
    lines.push('');
    lines.push('| # | ラベル | ルート | HTTP | title | console.err | pageerror | network err | screenshot |');
    lines.push('|---|---|---|---|---|---|---|---|---|');
    reports.forEach((r, i) => {
      const screenshotRel = path.relative(OUT_DIR, r.screenshotPath).replace(/\\/g, '/');
      const safeTitle = (r.title || '').replace(/\|/g, '\\|').slice(0, 30);
      lines.push(`| ${i + 1} | ${r.label} | \`${r.route}\` | ${r.status ?? 'N/A'} | ${safeTitle} | ${r.consoleErrors.length} | ${r.pageErrors.length} | ${r.networkErrors.length} | [📷](${screenshotRel}) |`);
    });

    lines.push('', '## 検出された問題の詳細', '');
    let hasAny = false;
    reports.forEach(r => {
      if (r.consoleErrors.length === 0 && r.pageErrors.length === 0 && r.networkErrors.length === 0) return;
      hasAny = true;
      lines.push(`### \`${r.route}\` (${r.label})`);
      if (r.pageErrors.length > 0) {
        lines.push('**🔴 page error (JS実行時例外)**:');
        r.pageErrors.forEach(e => lines.push(`- ${e}`));
      }
      if (r.consoleErrors.length > 0) {
        lines.push('**🔴 console.error**:');
        r.consoleErrors.forEach(e => lines.push(`- ${e}`));
      }
      if (r.networkErrors.length > 0) {
        lines.push('**🟡 network errors (4xx/5xx)**:');
        r.networkErrors.forEach(e => lines.push(`- ${e}`));
      }
      if (r.consoleWarnings.length > 0) {
        lines.push('**console.warning**:');
        r.consoleWarnings.forEach(e => lines.push(`- ${e}`));
      }
      lines.push('');
    });
    if (!hasAny) {
      lines.push('（問題は検出されませんでした）');
    }

    lines.push('## 次のアクション（Takeru判断）');
    lines.push('- [ ] 致命的なものを修正してもう一度 `/qa_UIUX` 回す');
    lines.push('- [ ] 軽微なのでリリース');
    lines.push('- [ ] `/codex review` で第二意見も取る');

    fs.writeFileSync(path.join(OUT_DIR, 'report.md'), lines.join('\n'));
    console.log(`✅ QA report saved: ${OUT_DIR}`);
    console.log(`Verdict: ${verdict}`);
  });
});
