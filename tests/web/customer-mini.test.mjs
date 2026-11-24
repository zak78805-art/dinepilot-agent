import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { existsSync, readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';

const require = createRequire(import.meta.url);
const root = process.env.SKY_MINIPROGRAM_ROOT || join(homedir(), 'Documents/Codex/reference/sky-take-out-sparse/mp-weixin');
const folder = join(root, 'pages/customer-assistant');
const hasMiniRepo = existsSync(join(folder, 'demo.js'));
const skipOpt = hasMiniRepo ? {} : { skip: 'Set SKY_MINIPROGRAM_ROOT to run companion WeChat mini-program tests' };
const demo = () => require(join(folder, 'demo.js'));

test('the registered native page has a navigator in the existing My page', skipOpt, () => {
  const config = JSON.parse(readFileSync(join(root, 'app.json'), 'utf8'));
  assert.ok(config.pages.includes('pages/customer-assistant/index'));
  assert.match(readFileSync(join(root, 'pages/my/my.wxml'), 'utf8'), /<navigator[^>]+url="\/pages\/customer-assistant\/index"/);
});

test('a two-person mild recommendation has two mains and respects the yuan budget', skipOpt, () => {
  const result = demo().answer('两个人，预算60元，不辣，帮我选餐');
  assert.equal(result.kind, 'meal');
  assert.equal(result.rows.filter(row => row.category === '主食').reduce((n, row) => n + row.qty, 0), 2);
  const total = result.rows.reduce((sum, row) => sum + row.price * row.qty, 0);
  assert.ok(total <= 6000);
  assert.equal(result.total, (total / 100).toFixed(2));
  assert.ok(result.rows.every(row => row.flavor === '不辣'));
  assert.match(result.note, /不含配送费和打包费/);
});

test('impossible and underspecified meal queries do not invent recommendations', skipOpt, () => {
  assert.equal(demo().answer('2人，10元，不辣').kind, 'text');
  assert.equal(demo().answer('帮我选餐').kind, 'text');
  assert.equal(demo().answer('我花生过敏，两人60元，帮我选餐').kind, 'text');
});

test('malformed money is not reinterpreted as a larger valid budget', skipOpt, () => {
  assert.equal(demo().answer('两个人，60.123元，不辣').kind, 'text');
});

test('recent demo orders have readable statuses and details with no contact info', skipOpt, () => {
  const result = demo().answer('查我的最近订单');
  assert.equal(result.kind, 'orders');
  assert.ok(result.orders.length >= 2);
  const order = demo().orderById(result.orders[0].id);
  assert.ok(order.lines.length);
  assert.equal(order.status, '配送中');
  assert.equal(demo().orderById('someone-else'), null);
  assert.doesNotMatch(JSON.stringify(order), /phone|address|consignee/);
});

test('write requests are not reported as completed actions', skipOpt, () => {
  const reply = demo().answer('帮我取消订单');
  assert.equal(reply.kind, 'text');
  assert.match(reply.text, /只支持查询/);
});

function pageWith(reply) {
  const { createPage } = require(join(folder, 'controller.js'));
  const page = createPage(reply);
  page.setData = function (data) { Object.assign(this.data, data); };
  return page;
}

test('failed send retains draft; retry sends once and clears draft only on success', skipOpt, async () => {
  let abort, complete, calls = 0;
  const page = pageWith(() => {
    calls++;
    return new Promise((resolve, reject) => { complete = resolve; abort = reject; });
  });
  page.onInput({ detail: { value: '查我的最近订单' } });
  const pending = page.onSend();
  await page.onSend();
  assert.equal(calls, 1);
  assert.equal(page.data.pending, true);
  abort(new Error('private detail'));
  await pending;
  assert.equal(page.data.draft, '查我的最近订单');
  assert.equal(page.data.messages.length, 0);
  assert.doesNotMatch(page.data.error, /private/);
  const retry = page.onSend();
  complete(demo().answer('查我的最近订单'));
  await retry;
  assert.equal(page.data.draft, '');
  assert.equal(page.data.messages.length, 1);
  assert.equal(page.data.pending, false);
});

test('new conversation ignores the old pending reply and resets the screen', skipOpt, async () => {
  let finish;
  const page = pageWith(() => new Promise(resolve => { finish = resolve; }));
  page.onInput({ detail: { value: '查我的最近订单' } });
  const old = page.onSend();
  page.onReset();
  finish(demo().answer('查我的最近订单'));
  await old;
  assert.deepEqual(page.data.messages, []);
  assert.equal(page.data.draft, '');
  assert.equal(page.data.pending, false);
});

test('order card opens its fixture details, then close and reset remove the sheet', skipOpt, async () => {
  const page = pageWith(async query => demo().answer(query));
  page.onPrompt({ currentTarget: { dataset: { id: 'order' } } });
  await page.onSend();
  const orders = page.data.messages[0].orders;
  page.onOrder({ currentTarget: { dataset: { id: orders[1].id } } });
  assert.equal(page.data.detail.number, orders[1].number);
  assert.ok(page.data.detail.lines.length);
  page.onCloseDetail();
  assert.equal(page.data.detail, null);
  page.onOrder({ currentTarget: { dataset: { id: orders[0].id } } });
  page.onReset();
  assert.equal(page.data.detail, null);
});
