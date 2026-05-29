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

    // 分析画面に直接アクセス
    await page.goto(`${BASE}/financials/${FD_ID}/analyze`, {
      waitUntil: 'domcontentloaded',
      timeout: 60000,
    });

    // print media に切り替え（CSS @media print 反映）
    await page.emulateMedia({ media: 'print' });

    // ネットワークアイドルとチャート描画を待つ
    try { await page.waitForLoadState('networkidle', { timeout: 30000 }); } catch (e) {}
    await page.waitForTimeout(3000);

    // 全タブを順番に開いて1ページのHTMLに展開（CSS で印刷時に全タブ表示）
    await page.addStyleTag({
      content: `
        /* A4横 297×210mm */
        @page { size: A4 landscape; margin: 10mm 12mm; }
        html, body { width: 100% !important; }
        /* container を 100% 幅に */
        .container, .container-fluid { max-width: 100% !important; padding: 0 !important; margin: 0 !important; }
        main, .row { margin: 0 !important; }
        /* 全タブを縦に並べて表示 */
        .cp-tab-panel { display: block !important; padding: 0 !important; }
        .cp-tabs, .navbar, .mode-switch { display: none !important; }
        /* ボタン類を非表示 */
        a.btn, button.btn, form .btn, #pdfBtn, .alert a.btn { display: none !important; }
        /* アコーディオン全開・矢印非表示 */
        details summary { cursor: default !important; pointer-events: none; list-style: none !important; }
        details summary::-webkit-details-marker { display: none !important; }
        details > summary { padding: 8px 12px !important; }
        details > div { padding: 12px !important; }
        /* 文字組み版（PDF用にやや大きめ） */
        body { line-height: 1.55; font-size: 13px; }
        h1 { font-size: 22px; } h2 { font-size: 19px; }
        h3 { font-size: 17px; } h4 { font-size: 15px; }
        h5 { font-size: 14px; } h6 { font-size: 13px; }
        .small { font-size: 11px !important; }
        .metric-card .metric-value { font-size: 19px !important; }
        .metric-card .metric-label { font-size: 11px !important; }
        .metric-card .metric-sub { font-size: 10px !important; }
        .metric-card { padding: 10px 12px !important; }
        .rk-hero { padding: 18px 22px !important; margin-bottom: 14px !important; }
        .rk-hero .rk-big-num, .rk-hero div[style*="font-size:1.4rem"] { font-size: 16px !important; }
        .rk-hero div[style*="font-size:1.1rem"], .rk-hero div[style*="font-size:1.05rem"] { font-size: 13px !important; }
        .rk-section-title { font-size: 15px !important; margin: 16px 0 10px !important; padding-bottom: 8px !important; }
        table.table-sm td, table.table-sm th { font-size: 11px !important; padding: 4px 6px !important; }
        .badge { font-size: 10px !important; }
        /* カードの余白詰める */
        .card { padding: 10px !important; margin-bottom: 8px !important; }
        .row.g-3 { gap: 8px 0 !important; }
        .col-md-3, .col-md-4, .col-md-6, .col-md-7, .col-md-5, .col-md-8 { padding: 4px !important; }
        /* 改ページ：見出しと続くカードをセットで */
        .rk-section-title { page-break-after: avoid; break-after: avoid; }
        h5, h6 { page-break-after: avoid; break-after: avoid; }
        /* カードは無理に1ページに収めない（余白防止） */
        .card { page-break-inside: auto; break-inside: auto; }
        details { page-break-inside: auto; break-inside: auto; }
        /* details summary は次のコンテンツと一緒に */
        details > summary { page-break-after: avoid; break-after: avoid; }
        /* ソリューションカード単体は分断防止（小さいので） */
        details .card { page-break-inside: avoid; break-inside: avoid; }
        /* 予想効果ボックスも分断防止 */
        details > div > div:last-child { page-break-inside: avoid; break-inside: avoid; }
        /* テーブル行は分断防止 */
        tr { page-break-inside: avoid; break-inside: avoid; }
        /* widow/orphan制御（最低3行は同じページに） */
        p, div { orphans: 3; widows: 3; }
        /* タブ間は強制改ページ（セクション切れ目を明確に） */
        .cp-tab-panel { page-break-before: always; break-before: page; }
        .cp-tab-panel:first-of-type { page-break-before: avoid; break-before: avoid; }
        /* データ制限警告（data-limit）を横並びに圧縮 */
        .data-limit { display: inline-block !important; margin: 4px 8px 4px 0 !important; padding: 6px 10px !important; font-size: 9px !important; }
        /* details panel タイトルの間隔詰める */
        details + details { margin-top: 4px !important; }
        /* キャンバス */
        canvas { max-width: 100% !important; }
        /* 円グラフ系は PDF で非表示（テーブルで代替）
           棒・線グラフは表示維持 */
        #chartSE, #chartRev, #chartCA, #chartCL, #costPie { display: none !important; }
        /* 円グラフが入ってた col-md-5 を非表示にして、テーブル側を100%幅に */
        .card:has(#chartSE) .col-md-5,
        .card:has(#chartRev) .col-md-5,
        .card:has(#chartCA) .col-md-5,
        .card:has(#chartCL) .col-md-5 { display: none !important; }
        .card:has(#chartSE) .col-md-7,
        .card:has(#chartRev) .col-md-7,
        .card:has(#chartCA) .col-md-7,
        .card:has(#chartCL) .col-md-7 { flex: 0 0 100% !important; max-width: 100% !important; padding: 0 !important; }
      `,
    });

    // 強制的に全パネルを表示
    await page.evaluate(() => {
      document.querySelectorAll('.cp-tab-panel').forEach(p => p.classList.add('active'));
      // 詳細アコーディオンも全開に
      document.querySelectorAll('details').forEach(d => d.setAttribute('open', ''));
      // ナビバー・操作系を強制非表示
      document.querySelectorAll('nav, .navbar').forEach(n => n.style.display = 'none');
      document.querySelectorAll('.cp-tabs, .mode-switch, #pdfBtn').forEach(el => el.style.display = 'none');
      document.querySelectorAll('.mb-3.d-flex.gap-2').forEach(el => el.style.display = 'none');
      document.querySelectorAll('.alert a.btn').forEach(el => el.style.display = 'none');
      document.querySelectorAll('a.btn[href*="referral"]').forEach(el => el.style.display = 'none');
      document.querySelectorAll('details summary').forEach(s => {
        const hint = s.querySelector('span.text-muted');
        if (hint && /クリック/.test(hint.textContent)) hint.style.display = 'none';
      });

      // 全 canvas に明示的なサイズを設定（PDF用）
      document.querySelectorAll('canvas').forEach(cv => {
        const parent = cv.parentElement;
        const w = parent ? parent.offsetWidth || 400 : 400;
        const id = cv.id;
        // 円グラフ系は正方形、棒・線グラフは横長
        let h = 220;
        if (['chartSE', 'chartRev', 'chartCA', 'chartCL', 'costPie'].includes(id)) {
          h = Math.min(w, 240); // 円は正方形に近く
        } else if (id === 'plStackBar' || id === 'plOpProfit') {
          h = 240;
        } else if (id === 'bsAssetBar' || id === 'bsLiabBar') {
          h = 240;
        } else if (id === 'trendRevProfit' || id === 'trendGrossMargin' || id === 'trendRecMonths' || id === 'trendInvMonths') {
          h = 200;
        }
        cv.style.maxHeight = h + 'px';
        cv.style.width = '100%';
        cv.style.height = h + 'px';
      });

      // 既存の Chart インスタンスを破棄してから（新サイズで再描画させる）
      if (window.Chart && window.Chart.instances) {
        const saved = Object.values(window.Chart.instances).map(c => ({
          canvasId: c.canvas && c.canvas.id,
          type: c.config.type,
          data: c.data,
          options: c.options,
        })).filter(x => x.canvasId);
        Object.values(window.Chart.instances).forEach(c => { try { c.destroy(); } catch(e) {} });
        // 新サイズで再生成
        saved.forEach(s => {
          const el = document.getElementById(s.canvasId);
          if (!el) return;
          try {
            new window.Chart(el, { type: s.type, data: s.data, options: s.options });
          } catch(e) { console.error('chart recreate failed:', s.canvasId, e.message); }
        });
      }
    });
    await page.waitForTimeout(2500);

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
