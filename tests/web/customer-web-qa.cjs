// Optional local browser check. Opens file:// and never listens on a port.
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { chromium } = require('playwright');

async function main() {
  const browser = await chromium.launch({
    headless: true,
    ...(process.env.SKY_BROWSER_PATH ? { executablePath: process.env.SKY_BROWSER_PATH } : {}),
  });
  try {
    const context = await browser.newContext({ viewport: { width: 1366, height: 850 } });
    const external = [];
    await context.route(/^https?:/, route => {
      external.push(route.request().url());
      return route.abort();
    });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
    await page.goto(pathToFileURL(path.resolve(__dirname, '../../web/customer/index.html')).href);
    assert.ok(await page.getByRole('heading', { name: '这顿饭，想怎么吃？' }).isVisible());
    assert.ok(await page.locator('.bento').evaluate(image => image.complete && image.naturalWidth > 0));
    const output = path.resolve(__dirname, '../../output/playwright');
    await page.screenshot({ path: path.join(output, 'customer-web-desktop.png') });
    await page.getByRole('button', { name: /帮我选餐/ }).click();
    const question = page.getByRole('textbox', { name: /输入选餐/ });
    assert.match(await question.inputValue(), /60 元/);
    await question.press('Enter');
    await page.getByText('¥53.00', { exact: true }).waitFor({ state: 'visible' });
    assert.ok(await page.getByText(/不含打包费和配送费/).isVisible());
    await page.setViewportSize({ width: 375, height: 812 });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({ path: path.join(output, 'customer-web-mobile.png'), fullPage: true });
    await page.getByRole('button', { name: /查我的订单/ }).click();
    await page.getByRole('button', { name: '发送' }).click();
    await page.getByRole('button', { name: '查看演示订单 002详情' }).click();
    const detail = page.getByRole('dialog', { name: '演示订单 002' });
    assert.ok(await detail.getByText('菌菇鸡肉饭').isVisible());
    assert.ok(await detail.getByText('配送中').isVisible());
    await page.keyboard.press('Escape');
    assert.equal(await detail.isVisible(), false);
    await page.getByRole('button', { name: '新对话' }).click();
    assert.equal(await page.locator('.turn').count(), 0);
    assert.equal(await question.inputValue(), '');
    assert.deepEqual(errors, []);
    assert.deepEqual(external, []);
    console.log('Local browser checks passed: desktop, mobile, meal, order detail, reset, zero HTTP requests.');
  } finally {
    await browser.close();
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
