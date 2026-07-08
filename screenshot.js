// スクショ撮影スクリプト
// 実行: node screenshot.js
const { chromium } = require('@playwright/test');
const fs = require('fs');
const path = require('path');

const BASE = 'http://127.0.0.1:8000';
const USER = 'test';
const PASS = 'test';
const OUT = path.resolve(__dirname, 'screenshots');
fs.mkdirSync(OUT, { recursive: true });

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();

  // ログイン
  await page.goto(`${BASE}/login`);
  await page.fill('input[name="username"]', USER);
  await page.fill('input[name="password"]', PASS);
  await page.click('button[type="submit"]');
  await page.waitForURL(/dashboard/);
  console.log('✓ login');

  // ダッシュボード
  await page.screenshot({ path: path.join(OUT, '01_dashboard.png'), fullPage: true });
  console.log('✓ dashboard');

  // クライアント画面（参考）
  await page.goto(`${BASE}/clients/8`);
  await page.waitForLoadState('networkidle');
  await page.screenshot({ path: path.join(OUT, '02_client.png'), fullPage: true });
  console.log('✓ client');

  // キャッシュ済みの分析画面に直接アクセス（fd_id=17）
  await page.goto(`${BASE}/financials/17/analyze`, { waitUntil: 'domcontentloaded', timeout: 60000 });
  try { await page.waitForLoadState('networkidle', { timeout: 30000 }); } catch(e) {}
  await page.waitForTimeout(2000);

  // ダミーチェック
  if (true) {

    // タブ切替してスクショ
    const tabs = ['overview', 'advice', 'services', 'subsidies', 'deepdive', 'hearing', 'simulator'];
    for (const t of tabs) {
      const el = page.locator(`.cp-tab[data-tab="${t}"]`);
      if (await el.count() > 0) {
        await el.click();
        await page.waitForTimeout(800);
        await page.screenshot({ path: path.join(OUT, `03_tab_${t}.png`), fullPage: true });
        console.log(`✓ tab ${t}`);
      }
    }

    // 社長モードでもovervieタブを撮影
    const simpleBtn = page.locator('#modeSwitch button[data-mode="simple"]');
    if (await simpleBtn.count() > 0) {
      await simpleBtn.click();
      await page.waitForTimeout(500);
      await page.locator('.cp-tab[data-tab="overview"]').click();
      await page.waitForTimeout(500);
      await page.screenshot({ path: path.join(OUT, '04_overview_simple.png'), fullPage: true });
      console.log('✓ owner mode');
    }
  }

  await browser.close();
  console.log(`\n📁 保存先: ${OUT}`);
})();
