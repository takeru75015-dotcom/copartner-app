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
// 紹介PDF等で別ページを PDF 化したい場合に指定
// (例: /financials/22/referral-kit/1_0?mode=pdf)
const TARGET_PATH = process.env.COPARTNER_TARGET_PATH || `/financials/${FD_ID}/pdf-view`;

if (!FD_ID || !SESSION || !OUTPUT) {
  console.error('Missing env vars');
  process.exit(1);
}

(async () => {
  const browser = await chromium.launch();
  try {
    // A4縦 (210×297mm) を 1.5x スケールで約 1190×1684px
    const ctx = await browser.newContext({
      viewport: { width: 1000, height: 1400 },
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

    // 対象ページ（デフォルト = 社長プレゼン版PDF、TARGET_PATH指定で紹介PDFなどに切替可）
    await page.goto(`${BASE}${TARGET_PATH}`, {
      waitUntil: 'domcontentloaded',
      timeout: 180000,
    });

    // print media に切り替え
    await page.emulateMedia({ media: 'print' });

    // ネットワークアイドルとチャート描画を待つ
    try { await page.waitForLoadState('networkidle', { timeout: 30000 }); } catch (e) {}
    await page.waitForTimeout(2000);

    // 全タブを順番に開いて1ページのHTMLに展開（CSS で印刷時に全タブ表示）
    // A4縦
    await page.addStyleTag({
      content: `
        @page { size: A4 portrait; margin: 12mm 12mm; }
        .navbar, .alert a.btn, button.btn, a.btn[href*="/clients"] { display: none !important; }
        /* グラフは縦並びにして小さめに */
        .pdfv { max-width: 100% !important; padding: 0 !important; }
      `,
    });

    // pdf_view.html はチャートなし・全展開済みなので追加処理不要
    await page.waitForTimeout(500);

    // PDF出力（A4縦）
    await page.pdf({
      path: OUTPUT,
      format: 'A4',
      landscape: false,
      printBackground: true,
      preferCSSPageSize: true,
      margin: { top: '12mm', bottom: '12mm', left: '12mm', right: '12mm' },
    });

    console.log('OK');
  } finally {
    await browser.close();
  }
})().catch(e => {
  console.error(e);
  process.exit(2);
});
