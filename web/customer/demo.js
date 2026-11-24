(function () {
  'use strict';
  // All data is fictional. This module has no network, credential or storage access.
  const menu = [
    { id: 'mushroom', name: '菌菇鸡肉饭', group: '主食', cents: 2400, flavor: '不辣', icon: '饭', tint: 'gold' },
    { id: 'tomato', name: '番茄滑蛋饭', group: '主食', cents: 2100, flavor: '不辣', icon: '蛋', tint: 'peach' },
    { id: 'greens', name: '蒜香时蔬', group: '小菜', cents: 800, flavor: '不辣', icon: '蔬', tint: 'mint' },
    { id: 'pepper', name: '青椒鸡丁饭', group: '主食', cents: 2500, flavor: '微辣', icon: '饭', tint: 'gold' },
    { id: 'cucumber', name: '微辣拍黄瓜', group: '小菜', cents: 800, flavor: '微辣', icon: '蔬', tint: 'mint' },
    { id: 'mala', name: '麻辣牛肉饭', group: '主食', cents: 2900, flavor: '辣', icon: '牛', tint: 'peach' },
    { id: 'tofu', name: '辣拌豆腐', group: '小菜', cents: 1000, flavor: '辣', icon: '豆', tint: 'gold' },
  ];
  const makeOrder = (id, title, time, status, tint, ids) => {
    const rows = ids.map(id => ({ ...menu.find(dish => dish.id === id), qty: 1 }));
    return { id, title, time, status, tint, rows, total: rows.reduce((sum, row) => sum + row.cents, 0) };
  };
  const orders = [
    makeOrder('demo-002', '演示订单 002', '12:18', '配送中', 'gold', ['mushroom', 'tomato']),
    makeOrder('demo-001', '演示订单 001', '11:34', '已完成', 'mint', ['pepper', 'cucumber']),
  ];
  const counts = { 一: 1, 两: 2, 二: 2, 三: 3, 四: 4, 五: 5, 六: 6 };
  const text = value => ({ kind: 'text', text: value });
  const yuan = cents => '¥' + (cents / 100).toFixed(2);

  function answer(raw) {
    const query = raw.trim().normalize('NFKC');
    const mild = /不(?:吃)?辣|不要辣|不放辣|免辣|清淡/.test(query);
    if (/帮我取消|取消订单|催单|帮我退款|发起退款|帮我支付|帮我下单|立即下单|购物车|改地址/.test(query)) {
      return text('这里仅演示查询和推荐，不会执行下单、支付、催单或取消订单。');
    }
    if (/过敏|忌口|清真|素食|不要|不吃|不放/.test(query.replace(/不吃辣|不要辣|不放辣/g, ''))) {
      return text('演示数据无法核实过敏原或其他饮食限制，请向商家确认原料和制作方式。');
    }
    if (/订单|配送到哪|买过/.test(query)) {
      return { kind: 'orders', orders: orders.map(({ rows, ...summary }) => ({ ...summary })) };
    }
    if (!/推荐|选餐|搭配|预算|吃|[人元块位¥]/.test(query)) {
      return text('我可以演示选餐或查订单。试试「两个人，预算 60 元，不辣」，或「查我的订单」。');
    }
    const count = query.match(/(?:^|[^\d一二两三四五六七八九十])([0-9]+|[一二两三四五六])\s*(?:个\s*)?(?:人|位)/);
    const amount = query.match(/(?<![\d.\-−])(?<!\d,)(\d+(?:\.\d{1,2})?)\s*(?:元|块)/)
      || query.match(/(?:预算\s*[:]?\s*¥?|¥)\s*(\d+(?:\.\d{1,2})?)(?![\d.]|,\d)/);
    const people = count && (counts[count[1]] || Number(count[1]));
    const budget = amount && Math.round(Number(amount[1]) * 100);
    if (!people || people > 6 || !budget || budget > 100000) {
      return text('请告诉我用餐人数（1–6 人）和预算（1,000 元以内），也可以补充口味。比如「两个人，预算 60 元，不辣」。');
    }
    const flavor = mild ? '不辣' : /微辣|少辣/.test(query) ? '微辣' : /辣/.test(query) ? '辣' : '不辣';
    const mains = menu.filter(dish => dish.group === '主食' && dish.flavor === flavor);
    const side = menu.find(dish => dish.group === '小菜' && dish.flavor === flavor);
    const least = Math.min(...mains.map(dish => dish.cents));
    if (least * people > budget) return text('演示菜单在这个预算内凑不齐每人一份主食。可以提高预算，或试试别的口味。');
    let total = 0;
    const rows = [];
    function add(dish) {
      const existing = rows.find(row => row.id === dish.id);
      if (existing) existing.qty++;
      else rows.push({ ...dish, qty: 1 });
      total += dish.cents;
    }
    for (let i = 0; i < people; i++) {
      const fits = dish => total + dish.cents + least * (people - i - 1) <= budget;
      add(mains.find(dish => fits(dish) && !rows.some(row => row.id === dish.id)) || mains.find(fits));
    }
    if (side && total + side.cents <= budget) add(side);
    return { kind: 'meal', people, flavor, budget, rows, total };
  }

  function orderById(id) {
    const order = orders.find(item => item.id === id);
    return order ? { ...order, rows: order.rows.map(row => ({ ...row })) } : null;
  }

  function createConversation(reply, onChange) {
    const state = { draft: '', pending: false, error: '', turns: [] };
    let generation = 0;
    return {
      state,
      draft(value) {
        if (state.pending) return;
        state.draft = value;
        state.error = '';
        onChange(state, 'draft');
      },
      async send() {
        const question = state.draft.trim();
        if (state.pending || !question) return;
        if (question.length > 280) {
          state.error = '请输入不超过 280 字的问题。';
          onChange(state, 'error');
          return;
        }
        const current = generation;
        state.pending = true;
        state.error = '';
        onChange(state, 'pending');
        try {
          const result = await reply(question);
          if (current !== generation) return;
          state.turns = [...state.turns, { question, result }].slice(-20);
          state.draft = '';
          state.pending = false;
          onChange(state, 'reply');
        } catch (_) {
          if (current !== generation) return;
          state.error = '没能生成结果，问题已保留，请重试。';
          state.pending = false;
          onChange(state, 'error');
        }
      },
      reset() {
        generation++;
        Object.assign(state, { draft: '', pending: false, error: '', turns: [] });
        onChange(state, 'reset');
      },
    };
  }

  const api = { answer, orderById, createConversation, yuan };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else window.CustomerDemo = api;
})();
