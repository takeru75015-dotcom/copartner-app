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
    // A4横 (297×210mm) を 1.5x スケールで約 1684×1190px
    const ctx = await browser.newContext({
      viewport: { width: 1400, height: 980 },
      deviceScaleFactor: 1.5,
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

    // PDF専用ビュー（社長プレゼン版）にアクセス
    await page.goto(`${BASE}/financials/${FD_ID}/pdf-view`, {
      waitUntil: 'domcontentloaded',
      timeout: 180000,  // 初回はAI生成で60秒以上かかる可能性
    });

    // print media に切り替え
    await page.emulateMedia({ media: 'print' });

    // ネットワークアイドルとチャート描画を待つ
    try { await page.waitForLoadState('networkidle', { timeout: 30000 }); } catch (e) {}
    await page.waitForTimeout(2000);

    // 全タブを順番に開いて1ページのHTMLに展開（CSS で印刷時に全タブ表示）
    // pdf_view.html は元から印刷用に設計されているので追加CSSは最小限
    await page.addStyleTag({
      content: `
        @page { size: A4 landscape; margin: 14mm 14mm; }
        .navbar, .alert a.btn, button.btn, a.btn[href*="/clients"] { display: none !important; }
      `,
    });

    // pdf_view.html はチャートなし・全展開済みなので追加処理不要
    await page.waitForTimeout(500);

    // PDF出力（A4横）
    await page.pdf({
      path: OUTPUT,
      format: 'A4',
      landscape: true,
      printBackground: true,
      preferCSSPageSize: true,
      margin: { top: '10mm', bottom: '10mm', left: '12mm', right: '12mm' },
    });

    console.log('OK');
  } finally {
    await browser.close();
  }
})().catch(e => {
  console.error(e);
  process.exit(2);
});
