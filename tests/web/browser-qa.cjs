// Run with bundled Playwright or NODE_PATH pointing to an installed playwright package.
const { chromium } = require('playwright');
const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '../../web');

(async () => {
  const server = http.createServer(async (req, res) => {
    if (req.url === '/sidebar-fixture') {
      res.setHeader('Content-Type', 'text/html; charset=utf-8');
      res.end('<html><body><div class="sidebar-container"><ul class="el-menu"><li>工作台</li></ul></div><script src="/assistant/sidebar-entry.js"></script></body></html>');
      return;
    }
    const file = path.resolve(root, '.' + decodeURIComponent(req.url).replace(/^\/assistant/, '') + (req.url.endsWith('/') ? 'index.html' : ''));
    if (!file.startsWith(root + path.sep)) { res.writeHead(404).end(); return; }
    try {
      const types = { '.html': 'text/html; charset=utf-8', '.mjs': 'application/javascript', '.js': 'application/javascript', '.css': 'text/css' };
      res.setHeader('Content-Type', types[path.extname(file)] || 'text/plain');
      res.end(await fs.readFile(file));
    } catch { res.writeHead(404).end(); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  let requests = 0;
  let status = 200;
  let lastRequest;
  let lastClear;
  await page.route('**/assistant-api/merchant/sessions/*', async route => {
    const request = route.request();
    lastClear = { method: request.method(), token: request.headers().token, path: new URL(request.url()).pathname };
    await route.fulfill({ status: 200, json: {
      agent_role: 'merchant', session_id: lastClear.path.split('/').pop(), cleared: true,
    } });
  });
  await page.route('**/assistant-api/chat', async route => {
    requests++;
    lastRequest = { body: route.request().postDataJSON(), headers: route.request().headers(), url: route.request().url() };
    await new Promise(resolve => setTimeout(resolve, 250));
    await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(status === 200 ? {
      session_id: lastRequest.body.session_id,
      answer: '## 查询事实\n\n| 日期 | 营业额（元） |\n| --- | ---: |\n| 8月4日 | **158** |\n\n<img src=x onerror="window.qaXss=true">\n\n### 分析限制\n非已完成订单2单，具体状态未知。',
      tool_calls: ['get_turnover_statistics', 'get_order_statistics', 'get_top_dishes'],
    } : { detail: 'not shown' }) });
  });
  try {
    const response = await page.goto(base + '/assistant/');
    assert.equal(response.status(), 200, 'assistant page must exist');
    await page.locator('#auth-notice').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#send').isDisabled(), true);
    assert.equal(requests, 0);
    await context.addCookies([{ name: 'token', value: 'qa-only-not-a-real-token', url: base }]);
    await page.reload();
    await page.locator('#question').fill('分析2026年8月1日至8月7日经营数据');
    await page.locator('#send').click();
    assert.equal(await page.locator('#new-session').isDisabled(), true);
    assert.equal(await page.locator('#send').isDisabled(), true);
    assert.equal(await page.locator('.quick-question').first().isDisabled(), true);
    await page.locator('.assistant-message table').waitFor();
    assert.equal(requests, 1);
    assert.equal(lastRequest.headers.token, 'qa-only-not-a-real-token');
    assert.ok(!lastRequest.url.includes('qa-only'));
    assert.equal(await page.locator('.tool-chip').count(), 3);
    assert.ok(!await page.locator('.tool-chip').first().evaluate(el => getComputedStyle(el, '::before').content.includes('✓')), 'attempted tools must not show a success tick');
    assert.equal(await page.locator('.assistant-message img').count(), 0);
    assert.equal(await page.evaluate(() => window.qaXss), undefined);
    assert.equal(await page.locator('#question').inputValue(), '');
    await fs.mkdir(path.resolve(__dirname, '../../output/playwright'), { recursive: true });
    await page.screenshot({ path: path.resolve(__dirname, '../../output/playwright/assistant-desktop.png'), fullPage: true });

    status = 502;
    await page.locator('#question').fill('保留我的问题');
    await page.locator('#send').click();
    await page.locator('.message-error').last().waitFor();
    assert.equal(await page.locator('#question').inputValue(), '保留我的问题');
    assert.equal(await page.locator('#send').isDisabled(), false);
    const oldSession = lastRequest.body.session_id;
    page.once('dialog', dialog => dialog.accept());
    await page.locator('#new-session').click();
    await page.locator('.assistant-message').first().waitFor({ state: 'detached' });
    assert.equal(await page.locator('.assistant-message').count(), 0);
    assert.deepEqual(lastClear, {
      method: 'DELETE', token: 'qa-only-not-a-real-token', path: `/assistant-api/merchant/sessions/${oldSession}`,
    });

    status = 401;
    await page.locator('#question').fill('重新查询');
    await page.locator('#send').click();
    await page.locator('#auth-notice').waitFor({ state: 'visible' });
    assert.ok((await page.locator('#auth-notice').innerText()).includes('登录'));
    assert.equal(await page.locator('#send').isDisabled(), true);
    assert.notEqual(lastRequest.body.session_id, oldSession);
    await context.addCookies([{ name: 'token', value: 'qa-new-login-token', url: base }]);
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    assert.equal(await page.locator('#send').isDisabled(), false);
    assert.equal(await page.locator('.message').count(), 0, 'credential changes clear previous account transcript');

    status = 200;
    await page.locator('#question').fill('旧账号的在途请求');
    await page.locator('#send').click();
    await context.addCookies([{ name: 'token', value: 'qa-third-login-token', url: base }]);
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    assert.equal(await page.locator('.message').count(), 0);
    await page.waitForFunction(() => !document.querySelector('#send').disabled);
    assert.equal(await page.locator('.assistant-message').count(), 0, 'late answer from prior credential is discarded');

    status = 401;
    await page.locator('#question').fill('旧账号的失败请求');
    await page.locator('#send').click();
    await context.addCookies([{ name: 'token', value: 'qa-fourth-login-token', url: base }]);
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    await page.waitForFunction(() => !document.querySelector('#send').disabled);
    assert.equal(await page.locator('#auth-notice').isHidden(), true, 'late 401 cannot blacklist the new credential');
    assert.equal(await page.locator('.message').count(), 0);

    await page.locator('#question').fill('即将退出');
    await context.clearCookies();
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    assert.equal(await page.locator('#send').isDisabled(), true);
    assert.equal(await page.locator('#question').inputValue(), '', 'logout clears the previous account draft');
    await context.addCookies([{ name: 'token', value: 'qa-fifth-login-token', url: base }]);
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));

    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: path.resolve(__dirname, '../../output/playwright/assistant-mobile.png'), fullPage: true });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'mobile page must not overflow horizontally');

    await page.goto(base + '/sidebar-fixture');
    await page.locator('#sky-agent-menu-entry').waitFor();
    await page.evaluate(() => { const menu = document.querySelector('.el-menu'); menu.innerHTML = '<li>Vue重绘</li>'; });
    await page.locator('#sky-agent-menu-entry').waitFor();
    assert.equal(await page.locator('#sky-agent-menu-entry').count(), 1);
    await page.evaluate(() => document.body.classList.add('hideSidebar'));
    assert.equal(await page.locator('.agent-entry-label').isVisible(), false);
    await page.evaluate(() => { location.hash = '#/login'; });
    await page.locator('#sky-agent-menu-entry').waitFor({ state: 'detached' });
    await page.evaluate(() => { location.hash = ''; });
    await page.locator('#sky-agent-menu-entry').waitFor();
    await page.locator('#sky-agent-menu-entry a').click();
    await page.waitForURL('**/assistant/');
    assert.deepEqual(errors, []);
    console.log('Browser QA passed: login/Markdown/XSS/concurrency/502/401/credential-switch/stale-replies/logout/new-session/mobile/sidebar/collapsed/login-route.');
  } catch (error) {
    console.error('UI diagnostics:', { requests, errors, status: await page.locator('#status').textContent().catch(() => ''), message: await page.locator('.message-error').count() ? await page.locator('.message-error').last().textContent() : '' });
    throw error;
  } finally {
    await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
