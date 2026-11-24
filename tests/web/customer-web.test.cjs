const test = require('node:test');
const assert = require('node:assert/strict');
const demo = () => require('../../web/customer/demo.js');

test('two mild mains and one side add up to ¥53, within the stated budget', () => {
  const result = demo().answer('两个人，预算 60 元，不辣');
  assert.equal(result.kind, 'meal');
  assert.equal(result.total, 5300);
  assert.equal(result.rows.reduce((sum, row) => sum + row.cents * row.qty, 0), 5300);
  assert.equal(result.rows.filter(row => row.group === '主食').reduce((n, row) => n + row.qty, 0), 2);
  assert.ok(result.rows.every(row => row.flavor === '不辣'));
});

test('meal selection respects tight and spicy requests', () => {
  const tight = demo().answer('两人，43元，不辣');
  assert.equal(tight.total, 4200);
  assert.equal(tight.rows[0].qty, 2);
  const spicy = demo().answer('1人，35元，微辣');
  assert.equal(spicy.total, 3300);
  assert.ok(spicy.rows.every(row => row.flavor === '微辣'));
});

test('missing, malformed and impossible constraints do not generate meals', () => {
  for (const prompt of ['帮我选餐', '两人，60.123元，不辣', '两人，-60元，不辣', '两人，10元，不辣', '两人60元，我花生过敏', '两人60元，不要鸡肉']) {
    assert.equal(demo().answer(prompt).kind, 'text', prompt);
  }
});

test('order results expose only synthetic summaries and allow matching detail', () => {
  const result = demo().answer('查我的订单');
  assert.equal(result.kind, 'orders');
  assert.equal(result.orders.length, 2);
  assert.equal(result.orders[0].status, '配送中');
  assert.equal(result.orders[0].rows, undefined);
  const details = demo().orderById(result.orders[0].id);
  assert.equal(details.rows.length, 2);
  assert.equal(details.total, 4500);
  assert.equal(demo().orderById('unknown'), null);
  assert.doesNotMatch(JSON.stringify(details), /phone|address|consignee/);
});

test('write action requests do not claim an action was done', () => {
  const result = demo().answer('帮我取消订单');
  assert.equal(result.kind, 'text');
  assert.match(result.text, /不会执行/);
});

test('request failure keeps the draft and ignores duplicate submit', async () => {
  let reject;
  let count = 0;
  const chat = demo().createConversation(() => {
    count++;
    return new Promise((_, fail) => { reject = fail; });
  }, () => {});
  chat.draft('查我的订单');
  const pending = chat.send();
  await chat.send();
  assert.equal(count, 1);
  reject(new Error('internal test detail'));
  await pending;
  assert.equal(chat.state.draft, '查我的订单');
  assert.equal(chat.state.turns.length, 0);
  assert.equal(chat.state.pending, false);
  assert.doesNotMatch(chat.state.error, /internal/);
});

test('reset discards late replies and clears drafts', async () => {
  let resolve;
  const chat = demo().createConversation(() => new Promise(done => { resolve = done; }), () => {});
  chat.draft('查我的订单');
  const pending = chat.send();
  chat.reset();
  chat.draft('新的问题');
  resolve(demo().answer('查我的订单'));
  await pending;
  assert.equal(chat.state.draft, '新的问题');
  assert.equal(chat.state.turns.length, 0);
  assert.equal(chat.state.pending, false);
});
