// PDF 出力スクリプト（Playwright）
// Python から spawn される。環境変数で制御:
//   COPARTNER_BASE: サーバURL (例: http://127.0.0.1:8000)
//   COPARTNER_FD_ID: financial_data ID
//   COPARTNER_SESSION: セッション cookie 値
//   COPARTNER_OUTPUT: 出力PDFパス

const { chromium } = require('@playwright/test');

const BASE = process.env.COPARTNER_BASE || 'http://127.0.0.1:8000';
const FD_ID = process.env.COPARTNER_FD_ID;
const SESSION = process.env.COPARTNER_SESSION;
const OUTPUT = process.env.COPARTNER_OUTPUT;

if (!FD_ID || !SESSION || !OUTPUT) {
  console.error('Missing env vars');
  process.exit(1);
}

(async () => {
  const browser = await chromium.launch();
  try {
    const ctx = await browser.newContext({
      viewport: { width: 1200, height: 1600 },
    });

    // セッション cookie をセット
    const url = new URL(BASE);
    await ctx.addCookies([{
      name: 'session',
      value: SESSION,
      domain: url.hostname,
      path: '/',
      httpOnly: true,
    }]);

    const page = await ctx.newPage();

    // 分析画面に直接アクセス
    await page.goto(`${BASE}/financials/${FD_ID}/analyze`, {
      waitUntil: 'domcontentloaded',
      timeout: 60000,
    });

    // ネットワークアイドルとチャート描画を待つ
    try { await page.waitForLoadState('networkidle', { timeout: 30000 }); } catch (e) {}
    await page.waitForTimeout(3000);

    // 全タブを順番に開いて1ページのHTMLに展開（CSS で印刷時に全タブ表示）
    await page.addStyleTag({
      content: `
        @media print {
          .cp-tab-panel { display: block !important; page-break-before: always; }
          .cp-tab-panel:first-of-type { page-break-before: auto; }
          .cp-tabs { display: none !important; }
          .navbar { display: none !important; }
          .mode-switch { display: none !important; }
          a.btn { display: none !important; }
          details { page-break-inside: avoid; }
        }
        /* PDFで非表示にしたい要素 */
        @page { size: A4; margin: 12mm 10mm; }
      `,
    });

    // 強制的に全パネルを表示
    await page.evaluate(() => {
      document.querySelectorAll('.cp-tab-panel').forEach(p => p.classList.add('active'));
      // 詳細アコーディオンも全開に
      document.querySelectorAll('details').forEach(d => d.setAttribute('open', ''));
    });
    await page.waitForTimeout(1500);

    // PDF出力
    await page.pdf({
      path: OUTPUT,
      format: 'A4',
      printBackground: true,
      margin: { top: '12mm', bottom: '12mm', left: '10mm', right: '10mm' },
    });

    console.log('OK');
  } finally {
    await browser.close();
  }
})().catch(e => {
  console.error(e);
  process.exit(2);
});
