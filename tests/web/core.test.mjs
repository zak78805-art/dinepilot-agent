import test from 'node:test';
import assert from 'node:assert/strict';
import {
  readToken,
  renderMarkdown,
  ChatClient,
  consumeSseBuffer,
  describeReportReview,
  normalizeReportReviewRef,
  normalizeReportReviewStatus,
  pollReportReview,
} from '../../web/core.mjs';

const ok = (answer = '完成') => ({ ok: true, status: 200, json: async () => ({ session_id: 's1', answer, tool_calls: ['get_order_statistics'] }) });

test('cookie token parsing preserves equals and rejects malformed header values', () => {
  assert.equal(readToken('x=1; token=abc%3Ddef%3D; theme=dark'), 'abc=def=');
  assert.equal(readToken('other_token=secret'), '');
  assert.equal(readToken('token=%ZZ'), '');
  assert.equal(readToken('token=abc%0Adef'), '');
});

test('model HTML and URLs stay inert while tables and emphasis render', () => {
  const html = renderMarkdown('## 查询事实\n\n| 日期 | 金额 |\n| --- | ---: |\n| 8月4日 | **158** |\n\n<img src=x onerror=alert(1)>\n[点击](javascript:alert(1))');
  assert.match(html, /<table>/);
  assert.match(html, /<th>日期<\/th>/);
  assert.match(html, /<strong>158<\/strong>/);
  assert.ok(!/<img|onerror="|<a\s|<script/i.test(html));
  assert.match(html, /&lt;img/);
});

test('fenced and inline code are escaped, not parsed as markup', () => {
  const html = renderMarkdown('`<script>x</script>`\n\n```html\n<img src=x>\n```');
  assert.match(html, /<code>&lt;script&gt;x&lt;\/script&gt;<\/code>/);
  assert.match(html, /<pre><code>&lt;img src=x&gt;<\/code><\/pre>/);
});

test('pathological model tables cannot expand into unbounded DOM cells', () => {
  const header = '|' + 'x|'.repeat(1000);
  const divider = '|' + '---|'.repeat(1000);
  const markdown = [header, divider, ...Array(1000).fill('|x|')].join('\n');
  const html = renderMarkdown(markdown);
  assert.ok((html.match(/<td>/g) || []).length <= 2000);
  assert.ok(html.length < 100000);
  assert.ok(html.includes('纯文本'));
});

test('cumulative cells across multiple tables are bounded', () => {
  const one = ['|a|b|', '|---|---|', ...Array(199).fill('|x|y|')].join('\n');
  const html = renderMarkdown(Array(6).fill(one).join('\n\n'));
  assert.ok((html.match(/<td>/g) || []).length <= 2000);
});

test('missing login does not send any request', async () => {
  let sent = 0;
  const client = new ChatClient({ getToken: () => '', fetchImpl: async () => { sent++; return ok(); } });
  await assert.rejects(client.send('分析订单', 's1'), e => e.code === 'AUTH_REQUIRED');
  assert.equal(sent, 0);
  assert.equal(client.busy, false);
});

test('request uses same-origin endpoint and current credential only in header', async () => {
  let request;
  const client = new ChatClient({ getToken: () => 'private-token', fetchImpl: async (...args) => { request = args; return ok(); } });
  const result = await client.send(' 分析订单 ', 's1');
  assert.equal(request[0], '/assistant-api/chat');
  assert.equal(request[1].headers.token, 'private-token');
  assert.deepEqual(JSON.parse(request[1].body), { session_id: 's1', message: '分析订单' });
  assert.equal(request[1].credentials, 'same-origin');
  assert.equal(result.answer, '完成');
  assert.equal(client.busy, false);
});

test('default browser fetch retains the global receiver', async () => {
  const original = globalThis.fetch;
  globalThis.fetch = function () { assert.equal(this, globalThis); return Promise.resolve(ok()); };
  try {
    const result = await new ChatClient({ getToken: () => 'x' }).send('查询', 's1');
    assert.equal(result.answer, '完成');
  } finally { globalThis.fetch = original; }
});

for (const [status, code] of [[401, 'AUTH_EXPIRED'], [403, 'FORBIDDEN'], [429, 'RATE_LIMIT'], [502, 'SERVICE_UNAVAILABLE']]) {
  test(`HTTP ${status} is shown as a safe actionable error`, async () => {
    const client = new ChatClient({ getToken: () => 'x', fetchImpl: async () => ({ ok: false, status, json: async () => ({ detail: 'secret traceback' }) }) });
    await assert.rejects(client.send('查询', 's1'), e => e.code === code && !e.message.includes('secret'));
    assert.equal(client.busy, false);
  });
}

test('double send cannot issue a second network request', async () => {
  let finish;
  let count = 0;
  const client = new ChatClient({ getToken: () => 'x', fetchImpl: () => { count++; return new Promise(resolve => { finish = resolve; }); } });
  const first = client.send('查询', 's1');
  await assert.rejects(client.send('重复', 's1'), e => e.code === 'BUSY');
  assert.equal(count, 1);
  finish(ok());
  await first;
});

test('timeout releases busy state without exposing native errors', async () => {
  const client = new ChatClient({ getToken: () => 'x', timeoutMs: 5, fetchImpl: (_, options) => new Promise((resolve, reject) => options.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))) });
  await assert.rejects(client.send('查询', 's1'), e => e.code === 'TIMEOUT');
  assert.equal(client.busy, false);
});

test('network failure becomes a readable network error', async () => {
  const client = new ChatClient({ getToken: () => 'x', fetchImpl: async () => { throw new TypeError('private network details'); } });
  await assert.rejects(client.send('查询', 's1'), e => e.code === 'NETWORK' && !e.message.includes('private'));
});

test('invalid JSON and mismatched session responses are rejected', async () => {
  for (const response of [
    { ok: true, json: async () => { throw new Error('json'); } },
    { ok: true, json: async () => ({ session_id: 'other', answer: '串答', tool_calls: [] }) },
    { ok: true, json: async () => ({ session_id: 's1', answer: null, tool_calls: [] }) },
    { ok: true, json: async () => null },
  ]) {
    const client = new ChatClient({ getToken: () => 'x', fetchImpl: async () => response });
    await assert.rejects(client.send('查询', 's1'), e => e.code === 'BAD_RESPONSE');
  }
});

test('timeout during response body read is reported as timeout, not invalid JSON', async () => {
  const client = new ChatClient({ getToken: () => 'x', timeoutMs: 5, fetchImpl: async (_, options) => ({
    ok: true,
    json: () => new Promise((resolve, reject) => options.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))),
  }) });
  await assert.rejects(client.send('查询', 's1'), e => e.code === 'TIMEOUT');
});

test('empty or oversized input is rejected before fetch', async () => {
  const client = new ChatClient({ getToken: () => 'x', fetchImpl: () => { assert.fail('must not send'); } });
  await assert.rejects(client.send('  ', 's1'), e => e.code === 'INVALID_INPUT');
  await assert.rejects(client.send('字'.repeat(4001), 's1'), e => e.code === 'INVALID_INPUT');
});

const clearedSession = (cleared = true) => ({
  ok: true, status: 200, json: async () => ({ agent_role: 'merchant', session_id: 's1', cleared }),
});

test('clear calls the merchant DELETE endpoint with token only in the header', async () => {
  let request;
  const client = new ChatClient({ getToken: () => 'offline-token', fetchImpl: async (...args) => {
    request = args; return clearedSession(false);
  } });
  assert.equal(await client.clearSession('s1'), false);
  assert.equal(request[0], '/assistant-api/merchant/sessions/s1');
  assert.equal(request[1].method, 'DELETE');
  assert.deepEqual(request[1].headers, { token: 'offline-token' });
  assert.equal(request[1].credentials, 'same-origin');
  assert.equal(request[1].body, undefined);
});

test('clear rejects invalid session IDs and missing credentials without sending', async () => {
  let token = 'offline-token';
  const client = new ChatClient({ getToken: () => token, fetchImpl: () => assert.fail('unexpected fetch') });
  for (const id of ['', 'a/b', 's1\n', 'a'.repeat(129)]) {
    await assert.rejects(() => client.clearSession(id), e => e.code === 'INVALID_SESSION');
  }
  token = '';
  await assert.rejects(() => client.clearSession('s1'), e => e.code === 'AUTH_REQUIRED');
});

test('clear rejects busy, unauthorized and malformed responses with safe errors', async () => {
  for (const [response, code] of [
    [{ ok: false, status: 409, json: async () => ({ detail: 'private details' }) }, 'SESSION_BUSY'],
    [{ ok: false, status: 401 }, 'AUTH_EXPIRED'],
    [{ ok: false, status: 503 }, 'SERVICE_UNAVAILABLE'],
    [{ ok: true, status: 200, json: async () => ({ agent_role: 'customer', session_id: 's1', cleared: true }) }, 'BAD_RESPONSE'],
    [{ ok: true, status: 200, json: async () => ({ agent_role: 'merchant', session_id: 's2', cleared: true }) }, 'BAD_RESPONSE'],
    [{ ok: true, status: 200, json: async () => ({ agent_role: 'merchant', session_id: 's1', cleared: 'true' }) }, 'BAD_RESPONSE'],
  ]) {
    const client = new ChatClient({ getToken: () => 'x', fetchImpl: async () => response });
    await assert.rejects(() => client.clearSession('s1'), e => e.code === code && !e.message.includes('private'));
    assert.equal(client.busy, false);
  }
});

test('clear pending blocks new chat and repeated clear, then unlocks on timeout', async () => {
  let count = 0;
  const client = new ChatClient({ getToken: () => 'x', timeoutMs: 10, fetchImpl: (_, { signal }) => {
    count++;
    return new Promise((_, reject) => signal.addEventListener('abort', () => reject(new Error('aborted'))));
  } });
  const pending = assert.rejects(() => client.clearSession('s1'), e => e.code === 'TIMEOUT');
  await assert.rejects(client.send('查询', 's1'), e => e.code === 'BUSY');
  await assert.rejects(() => client.clearSession('s1'), e => e.code === 'BUSY');
  await pending;
  assert.equal(count, 1);
  assert.equal(client.busy, false);
});

// Execute the page's actual event handlers without a browser or listening socket.
async function loadMerchantPage(t, fetchImpl, { confirm = () => true, cookie = 'token=offline-token' } = {}) {
  const nodes = new Map();
  const node = () => ({
    value: '', children: [], listeners: {}, classList: { add() {}, remove() {} },
    addEventListener(name, listener) { this.listeners[name] = listener; },
    replaceChildren() { this.children = []; },
    append(...children) { this.children.push(...children); },
    focus() {}, setAttribute() {}, remove() {},
  });
  const get = id => {
    if (!nodes.has(id)) nodes.set(id, node());
    return nodes.get(id);
  };
  const document = {
    cookie, getElementById: get, querySelectorAll: () => [], addEventListener() {},
    createElement: node, createTextNode: text => text,
  };
  const windowListeners = {};
  const window = { confirm, addEventListener(name, listener) { windowListeners[name] = listener; } };
  for (const [name, value] of Object.entries({ document, window, fetch: fetchImpl })) {
    const old = Object.getOwnPropertyDescriptor(globalThis, name);
    Object.defineProperty(globalThis, name, { value, configurable: true });
    t.after(() => {
      if (name === 'window' && windowListeners.pagehide) windowListeners.pagehide();
      return old ? Object.defineProperty(globalThis, name, old) : delete globalThis[name];
    });
  }
  await import(`../../web/app.mjs?test=${crypto.randomUUID()}`);
  return { get, document, click: () => get('new-session').listeners.click(),
    async send(text) {
      get('question').value = text;
      await get('chat-form').listeners.submit({ preventDefault() {} });
    } };
}

for (const cleared of [true, false]) {
  test(`page switches IDs only after a confirmed clear (${cleared})`, async t => {
    let finish;
    const ids = [];
    const page = await loadMerchantPage(t, (url, options) => {
      if (options.method === 'DELETE') {
        assert.equal(url, `/assistant-api/merchant/sessions/${ids[0]}`);
        return new Promise(resolve => { finish = resolve; });
      }
      const { session_id } = JSON.parse(options.body);
      ids.push(session_id);
      return { ok: true, json: async () => ({ session_id, answer: '完成', tool_calls: [] }) };
    });
    await page.send('旧问题');
    page.get('question').value = '旧草稿';
    const clearing = page.click();
    assert.equal(page.get('send').disabled, true);
    assert.equal(page.get('messages').children.length, 2);
    await page.click();
    finish({ ok: true, status: 200, json: async () => ({ agent_role: 'merchant', session_id: ids[0], cleared }) });
    await clearing;
    assert.equal(page.get('messages').children.length, 0);
    assert.equal(page.get('question').value, '');
    await page.send('新问题');
    assert.equal(ids.length, 2);
    assert.notEqual(ids[0], ids[1]);
  });
}

test('cancelled confirmation leaves old chat and draft untouched', async t => {
  const page = await loadMerchantPage(t, () => assert.fail('unexpected fetch'), { confirm: () => false });
  page.get('messages').append('old');
  page.get('question').value = '草稿';
  await page.click();
  assert.deepEqual(page.get('messages').children, ['old']);
  assert.equal(page.get('question').value, '草稿');
});

test('busy response retains the old ID, chat and draft', async t => {
  const ids = [];
  const page = await loadMerchantPage(t, (_, options) => {
    if (options.method === 'DELETE') return { ok: false, status: 409 };
    const { session_id } = JSON.parse(options.body);
    ids.push(session_id);
    return { ok: true, json: async () => ({ session_id, answer: '完成', tool_calls: [] }) };
  });
  await page.send('旧问题');
  page.get('question').value = '草稿';
  await page.click();
  assert.equal(page.get('messages').children.length, 2);
  assert.equal(page.get('question').value, '草稿');
  assert.match(page.get('status').textContent, /正在使用/);
  await page.send('接着问');
  assert.equal(ids.length, 2);
  assert.equal(ids[0], ids[1]);
});

test('a stale clear error after login changes does not lock the new account', async t => {
  let finish;
  const page = await loadMerchantPage(t, (_, options) => {
    assert.equal(options.headers.token, 'offline-token');
    return new Promise(resolve => { finish = resolve; });
  });
  const clearing = page.click();
  page.document.cookie = 'token=new-offline-token';
  finish({ ok: false, status: 401 });
  await clearing;
  assert.equal(page.get('send').disabled, false);
  assert.match(page.get('status').textContent, /凭证已变化/);
});

test('credential change during confirmation does not send a DELETE for the old session', async t => {
  const page = await loadMerchantPage(t, () => assert.fail('unexpected fetch'), { confirm: () => {
    page.document.cookie = 'token=new-offline-token';
    return true;
  } });
  page.get('messages').append('old');
  await page.click();
  assert.equal(page.get('messages').children.length, 0);
  assert.equal(page.get('send').disabled, false);
  assert.match(page.get('status').textContent, /凭证已变化/);
});

test('a clear network failure keeps page history and draft for manual retry', async t => {
  const page = await loadMerchantPage(t, () => { throw new Error('private network detail'); });
  page.get('messages').append('old');
  page.get('question').value = '未发送';
  await page.click();
  assert.deepEqual(page.get('messages').children, ['old']);
  assert.equal(page.get('question').value, '未发送');
  assert.match(page.get('status').textContent, /未确认清空/);
  assert.doesNotMatch(page.get('status').textContent, /private/);
  assert.equal(page.get('new-session').disabled, false);
});

function flattenText(node) {
  if (!node) return '';
  if (typeof node === 'string') return node;
  return [node.textContent || '', ...(node.children || []).map(flattenText)].join(' ');
}

function findByClass(node, className) {
  if (!node || typeof node === 'string') return null;
  if (String(node.className || '').split(/\s+/).includes(className)) return node;
  for (const child of node.children || []) {
    const found = findByClass(child, className);
    if (found) return found;
  }
  return null;
}

test('getReportReview does not lock ChatClient.busy and maps 404 or bad output to unavailable', async () => {
  let resolvePoll;
  let chatCalls = 0;
  const client = new ChatClient({
    getToken: () => 'merchant-token',
    fetchImpl: (url, options) => {
      if (options.method === 'GET') {
        assert.equal(url, '/assistant-api/merchant/sessions/s1/report-reviews/rev_12345678');
        assert.equal(options.headers.token, 'merchant-token');
        return new Promise(resolve => { resolvePoll = resolve; });
      }
      chatCalls += 1;
      return Promise.resolve(ok('并发提问成功'));
    },
  });
  const pollPromise = client.getReportReview('s1', 'rev_12345678');
  assert.equal(client.busy, false);
  const chatResult = await client.send('继续提问', 's1');
  assert.equal(chatResult.answer, '并发提问成功');
  assert.equal(chatCalls, 1);
  resolvePoll({ ok: false, status: 404 });
  const missing = await pollPromise;
  assert.equal(missing.status, 'unavailable');
  assert.equal(missing.failure, 'not_found');

  // Inconsistent pass with issues or revise with empty issues must never look passed.
  const badPass = normalizeReportReviewStatus({
    agent_role: 'merchant', session_id: 's1', review_id: 'rev_12345678',
    status: 'pass', issues: [{ code: 'other', quote: 'a', reason: 'b', suggestion: 'c', evidence_ids: ['orders'] }],
  }, 's1', 'rev_12345678');
  assert.equal(badPass.status, 'unavailable');
  assert.equal(badPass.failure, 'bad_output');

  const badRevise = normalizeReportReviewStatus({
    agent_role: 'merchant', session_id: 's1', review_id: 'rev_12345678',
    status: 'revise', issues: [],
  }, 's1', 'rev_12345678');
  assert.equal(badRevise.status, 'unavailable');
  assert.equal(badRevise.failure, 'bad_output');

  assert.equal(normalizeReportReviewRef({ status: 'pending', review_id: null }).status, 'unavailable');
  assert.equal(describeReportReview({ status: 'unavailable', failure: 'timeout' }).badge, '未完成复核');
});

test('pollReportReview stops at deadline with timeout unavailable and stops cleanly on abort', async () => {
  let clock = 0;
  let polls = 0;
  const client = new ChatClient({
    getToken: () => 'merchant-token',
    fetchImpl: async () => {
      polls += 1;
      return {
        ok: true, status: 200,
        json: async () => ({
          agent_role: 'merchant', session_id: 's1', review_id: 'rev_timeout1',
          status: 'running', issues: [], failure: null, evidence_ids: ['orders'],
        }),
      };
    },
  });
  const updates = [];
  const final = await pollReportReview({
    client,
    sessionId: 's1',
    reviewId: 'rev_timeout1',
    intervalMs: 100,
    deadlineMs: 250,
    now: () => clock,
    sleep: async ms => { clock += ms; },
    onUpdate: state => updates.push(state.status),
  });
  assert.equal(final.status, 'unavailable');
  assert.equal(final.failure, 'timeout');
  assert.ok(polls >= 1 && polls <= 4);
  assert.equal(updates.at(-1), 'unavailable');

  // Abort must stop without calling onUpdate.
  const controller = new AbortController();
  controller.abort();
  const abortedUpdates = [];
  const abortedResult = await pollReportReview({
    client,
    sessionId: 's1',
    reviewId: 'rev_timeout1',
    signal: controller.signal,
    onUpdate: state => abortedUpdates.push(state),
  });
  assert.equal(abortedResult, null);
  assert.deepEqual(abortedUpdates, []);
});

test('merchant page preserves original report when queue is full or review task disappears (404)', async t => {
  globalThis.__SKY_REVIEW_POLL_CONFIG__ = { intervalMs: 5, deadlineMs: 500 };
  t.after(() => { delete globalThis.__SKY_REVIEW_POLL_CONFIG__; });

  let turn = 0;
  let pollGate;
  const page = await loadMerchantPage(t, (url, options) => {
    if (options.method === 'POST') {
      turn += 1;
      const { session_id } = JSON.parse(options.body);
      if (turn === 1) {
        return {
          ok: true, status: 200,
          json: async () => ({
            session_id,
            answer: '报告一：复核队列满但报告先行展示。',
            tool_calls: ['get_order_statistics'],
            report_review: { review_id: null, status: 'unavailable', failure: 'queue_full' },
          }),
        };
      }
      return {
        ok: true, status: 200,
        json: async () => ({
          session_id,
          answer: '报告二：重启丢失任务仍保留原报告。',
          tool_calls: ['get_order_statistics'],
          report_review: { review_id: 'rev_lost_0001', status: 'pending', poll_after_ms: 5 },
        }),
      };
    }
    assert.ok(url.endsWith('/rev_lost_0001'));
    return new Promise(resolve => { pollGate = resolve; });
  });

  await page.send('报告一');
  await page.send('报告二');
  const articles = page.get('messages').children;
  const assistant1 = articles[1];
  const assistant2 = articles[3];
  assert.match(findByClass(assistant1, 'answer-body').innerHTML, /报告一/);
  assert.match(flattenText(findByClass(assistant1, 'report-review')), /未完成复核.*队列较忙/);
  assert.match(flattenText(findByClass(assistant2, 'report-review')), /等待复核/);

  await new Promise(resolve => setTimeout(resolve, 10));
  pollGate({ ok: false, status: 404 });
  await new Promise(resolve => setTimeout(resolve, 15));
  assert.match(flattenText(findByClass(assistant2, 'report-review')), /未完成复核.*已过期或服务已重启/);
  assert.match(findByClass(assistant2, 'answer-body').innerHTML, /报告二/);
});

test('merchant page updates pass/revise independently per report and discards late review after credential switch or clear', async t => {
  globalThis.__SKY_REVIEW_POLL_CONFIG__ = { intervalMs: 5, deadlineMs: 500 };
  t.after(() => { delete globalThis.__SKY_REVIEW_POLL_CONFIG__; });

  let currentSession = '';
  const pendingPolls = new Map();
  let turn = 0;

  const page = await loadMerchantPage(t, (url, options) => {
    if (options.method === 'DELETE') {
      return { ok: true, status: 200, json: async () => ({ agent_role: 'merchant', session_id: currentSession, cleared: true }) };
    }
    if (options.method === 'POST') {
      turn += 1;
      const { session_id } = JSON.parse(options.body);
      currentSession = session_id;
      if (turn === 3 || turn === 6) {
        return { ok: true, status: 200, json: async () => ({ session_id, answer: '普通问答，无复核', tool_calls: [], report_review: null }) };
      }
      const reviewId = `rev_case_000${turn}`;
      return {
        ok: true, status: 200,
        json: async () => ({
          session_id,
          answer: `报告${turn}原内容`,
          tool_calls: ['get_order_statistics'],
          report_review: { review_id: reviewId, status: 'pending', poll_after_ms: 5 },
        }),
      };
    }
    const reviewId = url.split('/').pop();
    return new Promise((resolve, reject) => {
      pendingPolls.set(reviewId, { resolve, reject, signal: options.signal });
      options.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
    });
  });

  await page.send('问题1');
  await page.send('问题2');
  await page.send('普通问候');
  await new Promise(resolve => setTimeout(resolve, 10));

  const articles = page.get('messages').children;
  const assistant1 = articles[1];
  const assistant2 = articles[3];
  const assistant3 = articles[5];

  // Ordinary chat has no review panel and triggered no review poll.
  assert.equal(findByClass(assistant3, 'report-review'), null);
  assert.equal(pendingPolls.size, 2);

  // Resolve report 2 first with untrusted XSS payload in quote/reason/suggestion -> must stay plain textContent!
  const originalHtml2 = findByClass(assistant2, 'answer-body').innerHTML;
  pendingPolls.get('rev_case_0002').resolve({
    ok: true, status: 200,
    json: async () => ({
      agent_role: 'merchant',
      session_id: currentSession,
      review_id: 'rev_case_0002',
      status: 'revise',
      issues: [{
        code: 'status_inference',
        quote: '<img src=x onerror=alert(1)>',
        reason: '<script>evil()</script>缺少取消数',
        suggestion: '建议改为取消数未知',
        evidence_ids: ['orders'],
      }],
      failure: null,
      evidence_ids: ['orders'],
    }),
  });
  await new Promise(resolve => setTimeout(resolve, 15));

  const reviewBox2 = findByClass(assistant2, 'report-review');
  assert.match(flattenText(reviewBox2), /建议检查/);
  assert.match(flattenText(reviewBox2), /<img src=x onerror=alert\(1\)>/);
  assert.equal(reviewBox2.innerHTML, undefined); // Rendered strictly via DOM nodes & textContent, never innerHTML.
  assert.equal(findByClass(assistant2, 'answer-body').innerHTML, originalHtml2);
  // Report 1 must still be waiting (no cross-talk!).
  assert.match(flattenText(findByClass(assistant1, 'report-review')), /等待复核/);

  // Resolve report 1 as pass.
  pendingPolls.get('rev_case_0001').resolve({
    ok: true, status: 200,
    json: async () => ({
      agent_role: 'merchant',
      session_id: currentSession,
      review_id: 'rev_case_0001',
      status: 'pass',
      issues: [],
      failure: null,
      evidence_ids: ['orders'],
    }),
  });
  await new Promise(resolve => setTimeout(resolve, 15));
  assert.match(flattenText(findByClass(assistant1, 'report-review')), /未发现问题/);

  // Start another report, then clear session -> poll signal must abort and late result must not appear.
  pendingPolls.clear();
  await page.send('问题4');
  await new Promise(resolve => setTimeout(resolve, 10));
  const poll4 = pendingPolls.get('rev_case_0004');
  assert.ok(poll4 && !poll4.signal.aborted);

  await page.click(); // Confirmed clear session.
  assert.equal(poll4.signal.aborted, true);
  assert.equal(page.get('messages').children.length, 0);

  // Start another report, then switch login credential -> poll signal must abort on focus/syncControls.
  pendingPolls.clear();
  await page.send('问题5');
  await new Promise(resolve => setTimeout(resolve, 10));
  const poll5 = pendingPolls.get('rev_case_0005');
  assert.ok(poll5 && !poll5.signal.aborted);

  page.document.cookie = 'token=another-merchant-token';
  await page.send('新账号问题'); // Triggers syncControls before submit and starts fresh conversation.
  assert.equal(poll5.signal.aborted, true);
});

test('ChatClient.decideApproval sends POST with admin header and maps 409/410 safely', async () => {
  let captured;
  const client = new ChatClient({
    getToken: () => 'merchant-token',
    fetchImpl: async (url, options) => {
      captured = { url, options };
      return {
        ok: true,
        status: 200,
        json: async () => ({
          agent_role: 'merchant',
          approval_id: 'apr_test_ticket_01',
          session_id: 's1',
          action: 'change_dish_status',
          status: 'executed',
          summary: '将菜品「酸菜鱼」状态由「在售」改为「停售」',
          target: { dish_id: 12, dish_name: '酸菜鱼', category: '招牌菜', price: 58 },
          before_state: { status: 1, status_name: '在售' },
          after_state: { status: 0, status_name: '停售' },
          created_at: '2026-11-24T16:00:00+08:00',
          expires_at: '2026-11-24T16:05:00+08:00',
          execution_message: '已确认执行',
        }),
      };
    },
  });
  const updated = await client.decideApproval('apr_test_ticket_01', 'confirm', { reason: '今日沽清' });
  assert.equal(updated.status, 'executed');
  assert.equal(captured.url, '/assistant-api/merchant/approvals/apr_test_ticket_01/confirm');
  assert.equal(captured.options.method, 'POST');
  assert.equal(captured.options.headers.token, 'merchant-token');
  assert.deepEqual(JSON.parse(captured.options.body), { reason: '今日沽清' });

  for (const [status, code] of [[409, 'APPROVAL_CONFLICT'], [410, 'APPROVAL_EXPIRED'], [404, 'APPROVAL_NOT_FOUND']]) {
    const failing = new ChatClient({ getToken: () => 'x', fetchImpl: async () => ({ ok: false, status }) });
    await assert.rejects(() => failing.decideApproval('apr_test_ticket_01', 'confirm'), e => e.code === code);
  }
});

test('merchant page renders HITL approval card safely and updates on confirm/reject', async t => {
  let decisionCalls = [];
  const page = await loadMerchantPage(t, (url, options) => {
    if (options.method === 'POST' && url === '/assistant-api/chat') {
      const { session_id } = JSON.parse(options.body);
      return {
        ok: true,
        status: 200,
        json: async () => ({
          session_id,
          answer: '已生成停售审批单，请点击确认。',
          tool_calls: ['request_dish_status_change'],
          approval_required: true,
          approval_id: 'apr_page_ticket_01',
          approval: {
            agent_role: 'merchant',
            approval_id: 'apr_page_ticket_01',
            session_id,
            action: 'change_dish_status',
            status: 'pending',
            summary: '将菜品「<img src=x onerror=alert(1)>」状态由「在售」改为「停售」',
            target: { dish_id: 12, dish_name: '<img src=x onerror=alert(1)>', category: '招牌菜', price: 58 },
            before_state: { status: 1, status_name: '在售' },
            after_state: { status: 0, status_name: '停售' },
            reason: '今日沽清',
            created_at: '2026-11-24T16:00:00+08:00',
            expires_at: '2026-11-24T16:05:00+08:00',
          },
        }),
      };
    }
    decisionCalls.push(url);
    return {
      ok: true,
      status: 200,
      json: async () => ({
        agent_role: 'merchant',
        approval_id: 'apr_page_ticket_01',
        session_id: 's1',
        action: 'change_dish_status',
        status: 'executed',
        summary: '将菜品「<img src=x onerror=alert(1)>」状态由「在售」改为「停售」',
        target: { dish_id: 12, dish_name: '<img src=x onerror=alert(1)>', category: '招牌菜', price: 58 },
        before_state: { status: 1, status_name: '在售' },
        after_state: { status: 0, status_name: '停售' },
        created_at: '2026-11-24T16:00:00+08:00',
        expires_at: '2026-11-24T16:05:00+08:00',
        execution_message: '已确认执行：状态已更新为停售。',
      }),
    };
  });

  await page.send('把酸菜鱼停售');
  const assistant = page.get('messages').children[1];
  const card = findByClass(assistant, 'approval-card');
  assert.ok(card);
  assert.equal(card.innerHTML, undefined); // Rendered strictly via DOM nodes & textContent.
  assert.match(flattenText(card), /待人工确认/);
  assert.match(flattenText(card), /在售 → 停售/);

  const confirmBtn = findByClass(card, 'approval-btn--confirm');
  assert.ok(confirmBtn);
  await confirmBtn.listeners.click();
  assert.deepEqual(decisionCalls, ['/assistant-api/merchant/approvals/apr_page_ticket_01/confirm']);
  assert.match(flattenText(card), /已确认执行/);
  assert.equal(findByClass(card, 'approval-btn--confirm'), null);
});

test('consumeSseBuffer parses split chunks and ChatClient.sendStream streams tool trace and deltas', async () => {
  const frames = [];
  let rem = consumeSseBuffer('event: tool_start\ndata: {"tool_name":"get_today_sales"}\n\nevent: del', (e, p) => frames.push([e, p]));
  assert.equal(frames.length, 1);
  rem = consumeSseBuffer(rem + 'ta\ndata: {"text":"你好"}\n\n', (e, p) => frames.push([e, p]));
  assert.equal(rem, '');
  assert.deepEqual(frames, [
    ['tool_start', { tool_name: 'get_today_sales' }],
    ['delta', { text: '你好' }],
  ]);

  const encoder = new TextEncoder();
  const sseChunks = [
    'event: meta\ndata: {"session_id":"s1","agent_role":"merchant"}\n\n',
    'event: tool_start\ndata: {"tool_call_id":"c1","tool_name":"get_today_sales"}\n\n',
    'event: tool_end\ndata: {"tool_call_id":"c1","tool_name":"get_today_sales","succeeded":true,"elapsed_ms":42.3}\n\n',
    'event: delta\ndata: {"text":"今日营业额"}\n\n',
    'event: delta\ndata: {"text":"158元。"}\n\n',
    'event: done\ndata: {"session_id":"s1","agent_role":"merchant","answer":"今日营业额158元。","tool_calls":["get_today_sales"],"metrics":{"ttft_ms":350,"total_ms":820}}\n\n',
  ];

  const seenEvents = [];
  const client = new ChatClient({
    getToken: () => 'stream-token',
    fetchImpl: async (url, options) => {
      assert.equal(url, '/assistant-api/chat');
      assert.match(options.headers.Accept, /text\/event-stream/);
      assert.equal(options.headers.token, 'stream-token');
      let idx = 0;
      return {
        ok: true,
        status: 200,
        headers: { get: name => (name.toLowerCase() === 'content-type' ? 'text/event-stream; charset=utf-8' : null) },
        body: {
          getReader() {
            return {
              async read() {
                if (idx >= sseChunks.length) return { done: true, value: undefined };
                return { done: false, value: encoder.encode(sseChunks[idx++]) };
              },
            };
          },
        },
      };
    },
  });

  const result = await client.sendStream('查今日数据', 's1', {
    onEvent: (name, payload) => seenEvents.push([name, payload]),
  });
  assert.equal(result.answer, '今日营业额158元。');
  assert.equal(result.metrics.ttft_ms, 350);
  assert.equal(client.busy, false);
  assert.deepEqual(seenEvents.map(([n]) => n), ['meta', 'tool_start', 'tool_end', 'delta', 'delta', 'done']);
});

test('merchant page renders live tool trace steps and streamed markdown via SSE', async t => {
  const encoder = new TextEncoder();
  const page = await loadMerchantPage(t, (_url, options) => {
    const { session_id } = JSON.parse(options.body);
    const sseChunks = [
      `event: meta\ndata: ${JSON.stringify({ session_id, agent_role: 'merchant' })}\n\n`,
      `event: tool_start\ndata: ${JSON.stringify({ tool_call_id: 'tc1', tool_name: 'get_today_sales' })}\n\n`,
      `event: tool_end\ndata: ${JSON.stringify({ tool_call_id: 'tc1', tool_name: 'get_today_sales', succeeded: true, elapsed_ms: 88 })}\n\n`,
      `event: delta\ndata: ${JSON.stringify({ text: '**今日营业额**：158元' })}\n\n`,
      `event: done\ndata: ${JSON.stringify({
        session_id, agent_role: 'merchant', answer: '**今日营业额**：158元',
        tool_calls: ['get_today_sales'], metrics: { ttft_ms: 400, total_ms: 1200 },
      })}\n\n`,
    ];
    let i = 0;
    return {
      ok: true,
      status: 200,
      headers: { get: name => (name.toLowerCase() === 'content-type' ? 'text/event-stream' : null) },
      body: {
        getReader() {
          return {
            async read() {
              if (i >= sseChunks.length) return { done: true, value: undefined };
              return { done: false, value: encoder.encode(sseChunks[i++]) };
            },
          };
        },
      },
    };
  });

  await page.send('查今日营业额');
  const assistant = page.get('messages').children[1];
  const trace = findByClass(assistant, 'tool-trace');
  assert.ok(trace);
  assert.match(flattenText(trace), /✓ 今日经营数据 · 88ms/);
  assert.match(findByClass(assistant, 'answer-body').innerHTML, /<strong>今日营业额<\/strong>：158元/);
  assert.match(page.get('status').textContent, /首字 0\.4 秒/);
});


test('ChatClient.sendStream distinguishes user abort from timeout and merchant page renders copy & regenerate controls', async t => {
  const externalController = new AbortController();
  const client = new ChatClient({
    getToken: () => 'abort-token',
    timeoutMs: 60000,
    fetchImpl: async (_url, options) => new Promise((_resolve, reject) => {
      options.signal.addEventListener('abort', () => reject(new Error('aborted')), { once: true });
    }),
  });
  const pendingPromise = client.sendStream('查今日数据', 's-abort', { signal: externalController.signal });
  externalController.abort();
  await assert.rejects(() => pendingPromise, error => error.code === 'ABORTED');
  assert.equal(client.busy, false);

  let sendCount = 0;
  const page = await loadMerchantPage(t, (_url, options) => {
    sendCount += 1;
    const { session_id } = JSON.parse(options.body);
    return {
      ok: true,
      status: 200,
      headers: { get: () => 'application/json' },
      json: async () => ({
        session_id,
        agent_role: 'merchant',
        answer: `第${sendCount}次回答内容`,
        tool_calls: ['get_today_sales'],
        trace_id: `trc_demo_000${sendCount}`,
      }),
    };
  });

  await page.send('查今天数据');
  const firstAssistant = page.get('messages').children[1];
  const actionsBar = findByClass(firstAssistant, 'message-actions');
  assert.ok(actionsBar);
  const buttons = actionsBar.children.filter(c => c.className === 'msg-action-btn');
  assert.equal(buttons.length, 2);
  assert.equal(buttons[0].textContent, '复制回答');
  assert.equal(buttons[1].textContent, '重新生成');
  assert.match(page.get('status').textContent, /Trace trc_demo_0001/);

  // Click regenerate -> issues a second chat request with the original prompt
  buttons[1].listeners.click();
  for (let i = 0; i < 20 && sendCount < 2; i++) {
    await new Promise(resolve => setTimeout(resolve, 10));
  }
  assert.equal(sendCount, 2);
});
