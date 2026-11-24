import test from 'node:test';
import assert from 'node:assert/strict';
import { CustomerChatClient, sanitizeUserToken } from '../../web/customer/live-core.mjs';

test('sanitizeUserToken strips optional Bearer prefix and rejects invalid header chars', () => {
  assert.equal(sanitizeUserToken('  Bearer user.jwt.token  '), 'user.jwt.token');
  assert.equal(sanitizeUserToken('plain-jwt'), 'plain-jwt');
  assert.equal(sanitizeUserToken('   '), '');
  assert.equal(sanitizeUserToken('bad\ntoken'), '');
});

test('CustomerChatClient calls customer endpoint with Bearer auth, omits cookies, and rejects role mismatch', async () => {
  let captured;
  const client = new CustomerChatClient({
    getToken: () => 'user-jwt-123',
    fetchImpl: async (url, options) => {
      captured = { url, options };
      return {
        ok: true,
        status: 200,
        json: async () => ({
          agent_role: 'customer',
          session_id: 'customer_s1',
          answer: '店铺当前营业中。',
          tool_calls: ['get_customer_shop_status'],
        }),
      };
    },
  });

  const result = await client.send('现在营业吗？', 'customer_s1');
  assert.equal(result.answer, '店铺当前营业中。');
  assert.equal(captured.url, '/assistant-api/customer/chat');
  assert.equal(captured.options.credentials, 'omit');
  assert.equal(captured.options.headers.Authorization, 'Bearer user-jwt-123');
  assert.equal(captured.options.headers.token, undefined);
  assert.deepEqual(JSON.parse(captured.options.body), { session_id: 'customer_s1', message: '现在营业吗？' });

  const merchantMismatch = new CustomerChatClient({
    getToken: () => 'user-jwt-123',
    fetchImpl: async () => ({
      ok: true,
      status: 200,
      json: async () => ({
        agent_role: 'merchant',
        session_id: 'customer_s1',
        answer: '串端响应',
        tool_calls: [],
      }),
    }),
  });
  await assert.rejects(() => merchantMismatch.send('查询', 'customer_s1'), e => e.code === 'BAD_RESPONSE');
});

test('CustomerChatClient clearSession calls customer DELETE endpoint and validates response', async () => {
  let captured;
  const client = new CustomerChatClient({
    getToken: () => 'user-jwt-123',
    fetchImpl: async (url, options) => {
      captured = { url, options };
      return {
        ok: true,
        status: 200,
        json: async () => ({ agent_role: 'customer', session_id: 'customer_s1', cleared: true }),
      };
    },
  });
  assert.equal(await client.clearSession('customer_s1'), true);
  assert.equal(captured.url, '/assistant-api/customer/sessions/customer_s1');
  assert.equal(captured.options.method, 'DELETE');
  assert.equal(captured.options.credentials, 'omit');
  assert.equal(captured.options.headers.Authorization, 'Bearer user-jwt-123');
});

test('CustomerChatClient rejects missing token and maps 401/409 safely', async () => {
  const noToken = new CustomerChatClient({
    getToken: () => '',
    fetchImpl: () => assert.fail('must not fetch without token'),
  });
  await assert.rejects(() => noToken.send('查订单', 'customer_s1'), e => e.code === 'AUTH_REQUIRED');

  const expired = new CustomerChatClient({
    getToken: () => 'expired',
    fetchImpl: async () => ({ ok: false, status: 401 }),
  });
  await assert.rejects(() => expired.send('查订单', 'customer_s1'), e => e.code === 'AUTH_EXPIRED');

  const busy = new CustomerChatClient({
    getToken: () => 'valid',
    fetchImpl: async () => ({ ok: false, status: 409 }),
  });
  await assert.rejects(() => busy.clearSession('customer_s1'), e => e.code === 'SESSION_BUSY');
});
