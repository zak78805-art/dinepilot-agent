(function () {
  'use strict';
  const { answer, orderById, createConversation, yuan } = window.CustomerDemo;
  const $ = id => document.getElementById(id);
  const field = $('question');
  const feed = $('feed');
  const dialog = $('details');
  const prompts = Array.from(document.querySelectorAll('[data-prompt]'));
  const node = (tag, name, text) => {
    const element = document.createElement(tag);
    if (name) element.className = name;
    if (text !== undefined) element.textContent = text;
    return element;
  };
  const summary = (name, subtitle) => {
    const row = node('span', 'item-copy');
    row.append(node('strong', '', name), node('small', '', subtitle));
    return row;
  };
  const badge = order => node('span', 'status-tag ' + order.tint, order.status);
  const scrollBottom = () => { feed.scrollTop = feed.scrollHeight; };

  function appendDishes(container, rows, total, label, note) {
    for (const dish of rows) {
      const row = node('div', 'dish');
      row.append(node('span', 'dish-icon ' + dish.tint, dish.icon),
        summary(dish.name, yuan(dish.cents) + ' × ' + dish.qty + ' · ' + dish.flavor),
        node('strong', 'price', yuan(dish.cents * dish.qty)));
      container.append(row);
    }
    const sum = node('div', 'total');
    sum.append(node('span', '', label), node('strong', 'price', yuan(total)));
    container.append(sum, node('p', 'dish-note', note));
  }

  function showOrder(id) {
    const order = orderById(id);
    if (!order || dialog.open) return;
    $('detail-title').textContent = order.title;
    const body = $('detail-body');
    const header = node('div', 'detail-status');
    header.append(badge(order), node('span', 'dish-note', '模拟时间 · ' + order.time));
    body.replaceChildren(header);
    appendDishes(body, order.rows, order.total, '演示订单金额', '仅用于体验，不代表真实配送或支付状态。');
    dialog.showModal();
  }

  function renderTurn({ question, result }) {
    const turn = node('article', 'turn');
    const card = node('div', 'answer');
    turn.append(node('p', 'bubble', question), node('p', 'reply-label', '✦  点餐助手 · 模拟结果'), card);
    if (result.kind === 'meal') {
      card.append(node('h3', '', '这一餐，给你配好了'),
        node('p', 'sub', `${result.people} 人 · ${result.flavor} · 预算 ${yuan(result.budget)}`));
      appendDishes(card, result.rows, result.total, '商品小计', '模拟商品小计，不含打包费和配送费，不代表真实库存或最终实付。');
    } else if (result.kind === 'orders') {
      card.append(node('h3', '', '最近订单'), node('p', 'sub', '以下是虚构订单，点击可查看明细。'));
      for (const order of result.orders) {
        const button = node('button', 'order');
        button.type = 'button';
        button.setAttribute('aria-label', '查看' + order.title + '详情');
        button.addEventListener('click', () => showOrder(order.id));
        button.append(summary(order.title, order.time + ' · ' + yuan(order.total)), badge(order), node('span', 'arrow', '›'));
        card.append(button);
      }
    } else card.append(node('p', '', result.text));
    return turn;
  }

  function update(state, event) {
    if (field.value !== state.draft) field.value = state.draft;
    field.disabled = state.pending;
    $('send').disabled = state.pending || !state.draft.trim();
    $('working').hidden = !state.pending;
    $('error').hidden = !state.error;
    $('error').textContent = state.error;
    $('welcome').hidden = state.pending || state.turns.length > 0;
    for (const button of prompts) button.disabled = state.pending;
    if (event === 'reply' || event === 'reset') $('turns').replaceChildren(...state.turns.map(renderTurn));
    if (event === 'reply' || event === 'pending') scrollBottom();
    if (event === 'reset') {
      if (dialog.open) dialog.close();
      $('detail-body').replaceChildren();
      feed.scrollTop = 0;
    }
  }
  const chat = createConversation(async question => {
    await new Promise(resolve => setTimeout(resolve, 270));
    return answer(question);
  }, update);

  field.addEventListener('input', () => chat.draft(field.value));
  field.addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
      event.preventDefault();
      $('composer').requestSubmit();
    }
  });
  $('composer').addEventListener('submit', async event => {
    event.preventDefault();
    await chat.send();
  });
  for (const button of prompts) button.addEventListener('click', () => {
    chat.draft(button.dataset.prompt);
    field.focus();
  });
  $('reset').addEventListener('click', () => { chat.reset(); field.focus(); });
  $('close-details').addEventListener('click', () => dialog.close());
  update(chat.state, 'reset');
})();
