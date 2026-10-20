// Usage: node shot.js page.html out_prefix frame1 frame2 ...   (or "all" for every frame)
const { chromium } = require('playwright');
(async () => {
  const [page_, prefix, ...frames] = process.argv.slice(2);
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1000, height: 420 }, deviceScaleFactor: 1 });
  let list = frames;
  if (frames[0] === 'all') {
    await page.goto('file://' + page_);
    const n = await page.evaluate(() => frames.length);
    list = [...Array(n).keys()].map(String);
  }
  for (const f of list) {
    await page.goto(`file://${page_}?hide-controls&frame=${f}`);
    await page.screenshot({ path: `${prefix}${String(f).padStart(4, '0')}.png`, fullPage: true });
  }
  await browser.close();
})();
