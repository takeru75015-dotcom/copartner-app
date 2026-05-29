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
        @page { size: A4; margin: 14mm 10mm; }
        /* 全タブを縦に並べて表示 */
        .cp-tab-panel { display: block !important; }
        .cp-tabs { display: none !important; }
        .navbar { display: none !important; }
        .mode-switch { display: none !important; }
        /* ボタン類を非表示 */
        a.btn, button.btn, form .btn, #pdfBtn { display: none !important; }
        .d-flex.gap-2:has(form) { display: none !important; }
        /* アコーディオン「▼ クリック」を非表示にして常時開く */
        details summary span.text-muted:last-child { display: none !important; }
        details summary { cursor: default !important; pointer-events: none; }
        details { page-break-inside: avoid; break-inside: avoid; }
        /* 見出しの孤立防止（直後の内容と一緒に保つ） */
        h1, h2, h3, h4, h5, h6, .rk-section-title { page-break-after: avoid; break-after: avoid; }
        /* カード単位で改行を避ける */
        .card { page-break-inside: avoid; break-inside: avoid; }
        /* テーブル行の分断防止 */
        tr { page-break-inside: avoid; break-inside: avoid; }
        /* 黒ヒーローセクションがページに収まるよう */
        .rk-hero { page-break-inside: avoid; break-inside: avoid; }
        /* 文字組み版（行間・禁則） */
        body { line-height: 1.65; word-break: keep-all; overflow-wrap: anywhere; }
        h6, h5 { white-space: normal !important; }
        /* タブ間に薄い区切り */
        .cp-tab-panel + .cp-tab-panel { border-top: 2px solid #0F0F19; padding-top: 24px; margin-top: 24px; }
        /* キャンバスを適切サイズに */
        canvas { max-width: 100% !important; height: auto !important; }
      `,
    });

    // 強制的に全パネルを表示
    await page.evaluate(() => {
      document.querySelectorAll('.cp-tab-panel').forEach(p => p.classList.add('active'));
      // 詳細アコーディオンも全開に
      document.querySelectorAll('details').forEach(d => d.setAttribute('open', ''));
      // ナビバー・操作系を強制非表示（インラインstyle対策）
      document.querySelectorAll('nav, .navbar').forEach(n => n.style.display = 'none');
      document.querySelectorAll('.cp-tabs, .mode-switch, #pdfBtn').forEach(el => el.style.display = 'none');
      // 「← クライアントに戻る」「再分析」「PDF出力」ボタン群の親 div を消す
      document.querySelectorAll('.mb-3.d-flex.gap-2').forEach(el => el.style.display = 'none');
      // 業種判定アラートのリンクボタンも消す
      document.querySelectorAll('.alert a.btn').forEach(el => el.style.display = 'none');
      // 申し込みボタン類も消す
      document.querySelectorAll('a.btn[href*="referral"]').forEach(el => el.style.display = 'none');
      // 「▼ クリックで開閉」のヒントテキストを消す
      document.querySelectorAll('details summary').forEach(s => {
        const hint = s.querySelector('span.text-muted');
        if (hint && /クリック/.test(hint.textContent)) hint.style.display = 'none';
      });
    });
    // チャートの再描画完了を待つ（ウィンドウ幅が変わってる可能性）
    await page.waitForTimeout(3000);

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
