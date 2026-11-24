// Self-contained interactive GitHub Pages showcase for DinePilot Agent (zero backend required).
const $ = id => document.getElementById(id);

const escapeHtml = value => String(value).replace(/[&<>"']/g, ch => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
));

const inlineMd = value => escapeHtml(value).replace(
  /`([^`]+)`|\*\*([^*]+)\*\*/g,
  (_, code, strong) => (code !== undefined ? `<code>${code}</code>` : `<strong>${strong}</strong>`),
);

function renderSafeMarkdown(text) {
  const lines = String(text).replace(/\r\n?/g, '\n').split('\n');
  const out = [];
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (!line.trim()) continue;
    if (line.includes('|') && i + 1 < lines.length && /^\|?[\s:|-]+\|?$/.test(lines[i + 1]) && lines[i + 1].includes('---')) {
      const splitRow = row => row.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim());
      const headers = splitRow(line);
      i += 1;
      const rows = [];
      while (i + 1 < lines.length && lines[i + 1].includes('|') && lines[i + 1].trim()) {
        rows.push(splitRow(lines[++i]));
      }
      out.push(
        `<table><thead><tr>${headers.map(h => `<th>${inlineMd(h)}</th>`).join('')}</tr></thead>`
        + `<tbody>${rows.map(r => `<tr>${headers.map((_, idx) => `<td>${inlineMd(r[idx] || '')}</td>`).join('')}</tr>`).join('')}</tbody></table>`,
      );
    } else if (/^#{1,4}\s/.test(line)) {
      out.push(`<h4>${inlineMd(line.replace(/^#+\s+/, ''))}</h4>`);
    } else if (/^\s*[-*]\s/.test(line)) {
      const items = [line.replace(/^\s*[-*]\s+/, '')];
      while (i + 1 < lines.length && /^\s*[-*]\s/.test(lines[i + 1])) items.push(lines[++i].replace(/^\s*[-*]\s+/, ''));
      out.push(`<ul>${items.map(item => `<li>${inlineMd(item)}</li>`).join('')}</ul>`);
    } else {
      out.push(`<p>${inlineMd(line)}</p>`);
    }
  }
  return out.join('\n');
}

function makeTraceId() {
  const bytes = new Uint8Array(6);
  crypto.getRandomValues(bytes);
  return 'trc_' + [...bytes].map(b => b.toString(16).padStart(2, '0')).join('');
}

function sleep(ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) { reject(new DOMException('Aborted', 'AbortError')); return; }
    const onAbort = () => { clearTimeout(t); reject(new DOMException('Aborted', 'AbortError')); };
    const t = setTimeout(() => {
      if (signal) signal.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    if (signal) signal.addEventListener('abort', onAbort, { once: true });
  });
}

// ---------------- Tab Switching ----------------
for (const tabBtn of document.querySelectorAll('.dp-tab')) {
  tabBtn.addEventListener('click', () => {
    const target = tabBtn.dataset.tab;
    for (const btn of document.querySelectorAll('.dp-tab')) {
      const active = btn.dataset.tab === target;
      btn.classList.toggle('active', active);
      btn.setAttribute('aria-selected', String(active));
    }
    for (const panel of document.querySelectorAll('.dp-panel')) {
      panel.hidden = panel.id !== `panel-${target}`;
    }
  });
}

// ---------------- Telemetry Inspector ----------------
const logBox = $('ins-logs');
let logCleared = false;

function appendTelemetryLog(obj) {
  if (!logCleared) {
    logBox.replaceChildren();
    logCleared = true;
  }
  const entry = document.createElement('div');
  entry.className = 'dp-log-entry';
  entry.textContent = JSON.stringify(obj);
  logBox.append(entry);
  logBox.scrollTop = logBox.scrollHeight;
}

function updateInspector({ traceId, ttft, revTok }) {
  if (traceId) $('ins-trace').textContent = traceId;
  if (ttft !== undefined) $('ins-ttft').textContent = ttft;
  if (revTok !== undefined) $('ins-rev-tok').textContent = revTok;
}

// ---------------- Merchant Scenarios ----------------
const MERCHANT_SCENARIOS = {
  parallel: {
    prompt: '对比最近7天与前7天的经营情况，并发拉取营业额、订单完成率和热销商品做环比诊断。',
    tools: [
      { name: '并行环比经营诊断 (5路 asyncio.gather)', raw: 'get_period_comparison_report', ms: 131, ok: true },
    ],
    ttftMs: 410,
    tokens: { input: 742, output: 248, total: 990 },
    answer: `#### 核心结论
本期（最近7天）已完成订单营业额 **¥880.50**，较等长上期（¥620.00）增长 **+¥260.50（+42.02%）**；订单完成率由 **80.00%** 提升至 **90.00%（+10.00 个百分点）**。

#### 关键指标环比（精确整数分 / 万分比计算）
| 指标 | 本期（含今天） | 上期（完整7天） | 环比变化 |
| --- | --- | --- | --- |
| 已完成订单营业额（status=5） | ¥880.50 | ¥620.00 | +¥260.50（+42.02%） |
| 全部状态总订单 / 已完成 | 40 单 / 36 单 | 30 单 / 24 单 | +10 单 / +12 单 |
| 订单完成率 | 90.00% | 80.00% | +10.00 个百分点 |
| 销量榜前二 | 招牌酸菜鱼(18份)、宫保鸡丁(12份) | — | 仅计已完成订单 |

#### 统计口径与分析限制
- 本期包含今天，当日尚未结束，与完整7天上期对比存在时间口径差异。
- 营业额仅计已完成订单金额合计，不等于净利润；非已完成订单（4单）未提供取消/拒单明细，不得直接推断为取消单。`,
    review: {
      mode: 'llm_pass',
      revTok: '912 tok (LLM)',
      finalStatus: 'pass',
      summary: '基于本轮聚合工具证据核对未指出问题（已完成口径、当日未结束提示、精确环比值均一致）。',
      issues: [],
    },
  },

  hitl: {
    prompt: '帮我发起将「酸菜鱼」设为停售的审批单，原因是今日食材售罄。',
    tools: [
      { name: '菜品状态审批 (仅只读查询，0写库)', raw: 'request_dish_status_change', ms: 42, ok: true },
    ],
    ttftMs: 320,
    tokens: { input: 510, output: 118, total: 628 },
    answer: `已为您生成**菜品状态变更待审批单**，当前**尚未修改数据库**。

- 目标菜品：**招牌酸菜鱼**（ID: \`12\` · 招牌菜 · ¥58）
- 状态变更：**起售 → 停售**
- 发起原因：今日食材售罄

请核对下方审批卡片并点击「确认执行」后变更才会生效（有效期 5 分钟，支持 Redis \`SET NX PX\` 并发防重与乐观状态校验）。`,
    approval: {
      approval_id: 'apr_live_demo_8921',
      dish_name: '招牌酸菜鱼',
      category: '招牌菜',
      price: 58,
      before: '起售',
      after: '停售',
      reason: '今日食材售罄',
    },
    review: null,
  },

  hallucination: {
    prompt: '演示：如果模型草稿把 0 订单写成“完成率 0% 表现差”，且把营业额等同于净利润，复核系统怎么拦截？',
    tools: [
      { name: '订单统计', raw: 'get_order_statistics', ms: 84, ok: true },
      { name: '营业额趋势', raw: 'get_turnover_statistics', ms: 79, ok: true },
    ],
    ttftMs: 360,
    tokens: { input: 630, output: 140, total: 770 },
    answer: `#### 模拟待复核草稿（故意注入 2 处典型口径幻觉）
- 统计区间内总订单为 0 单，**订单完成率为0%**，说明店铺经营异常。
- 历史区间内已完成订单**营业额等于净利润**，共计 0 元。

*(原草稿保留不篡改，请查看下方 0.031ms 确定性规则预检出的纠偏卡片)*`,
    review: {
      mode: 'precheck_revise',
      revTok: '0 tok (0.031ms 预检)',
      finalStatus: 'revise',
      summary: '0-Token 整数分/万分比规则在 0.031ms 内命中 2 处硬矛盾（未调用复核大模型），原报告已保留：',
      issues: [
        {
          code: '其他待核对',
          evidence: 'orders',
          quote: '订单完成率为0%',
          reason: '总订单数为0时完成率无定义，后端返回0不能直接表述为完成率0%。',
          suggestion: '建议改为「总订单为0，完成率不适用」。',
        },
        {
          code: '夸大推断',
          evidence: 'turnover',
          quote: '营业额等于净利润',
          reason: '营业额仅统计当前已完成订单(status=5)金额合计，不等于利润或全部已支付金额。',
          suggestion: '建议明确表述为「已完成订单金额合计，不代表利润」。',
        },
      ],
    },
  },

  'stream-stop': {
    prompt: '生成一份详细的月度经营复盘与改进建议（可随时点击红色「停止生成」测试中断与保留部分正文）。',
    tools: [
      { name: '营业额趋势', raw: 'get_turnover_statistics', ms: 96, ok: true },
      { name: '订单统计', raw: 'get_order_statistics', ms: 108, ok: true },
      { name: '商品销量排名', raw: 'get_top_dishes', ms: 121, ok: true },
    ],
    ttftMs: 390,
    slowStream: true,
    tokens: { input: 890, output: 380, total: 1270 },
    answer: `#### 1. 查询事实（按下单时间口径）
- 已完成订单营业额合计 **¥3,420.00**，共完成 **68 单**（总订单 75 单，完成率 **90.67%**，非已完成 7 单）。
- 销量前三为：招牌酸菜鱼（29份）、宫保鸡丁（19份）、米饭（52份）。

#### 2. 分析限制（未核实部分严禁臆测）
- 销量榜仅返回已完成订单前 10 项，不能据此推断全店在售品类或库存余量。
- 其余 7 单非已完成订单未查询状态明细，无法区分取消、待接单或派送中。
- 未提供食材成本与平台抽佣数据，无法计算毛利或净利润。

#### 3. 可进一步核对的建议
- 如需排查 7 笔非已完成订单的原因，可进一步调用 \`get_order_list(status=6)\` 核对取消原因字段。
- 可结合 \`get_period_comparison_report\` 对比上一等长周期变化，验证核心菜品复购稳定性。`,
    review: {
      mode: 'llm_pass',
      revTok: '884 tok (LLM)',
      finalStatus: 'pass',
      summary: '事实、限制、建议三段清晰分离，未出现无证据归因或口径混用。',
      issues: [],
    },
  },
};

function resolveMerchantScenario(text) {
  if (/审批|停售|起售|酸菜鱼|下架/.test(text)) return MERCHANT_SCENARIOS.hitl;
  if (/幻觉|复核|净利润|0%|纠偏/.test(text)) return MERCHANT_SCENARIOS.hallucination;
  if (/停止|月度|详细|复盘/.test(text)) return MERCHANT_SCENARIOS['stream-stop'];
  return MERCHANT_SCENARIOS.parallel;
}

const mMessages = $('m-messages');
const mConversation = $('m-conversation');
const mStatus = $('m-status');
const mInput = $('m-input');
const mSend = $('m-send');
const mStop = $('m-stop');
let mPending = false;
let mAbortController = null;

function scrollBox(el) { el.scrollTop = el.scrollHeight; }

function createMsg(container, role, text = '') {
  const wrap = document.createElement('div');
  wrap.className = `dp-msg dp-msg--${role}`;
  const head = document.createElement('div');
  head.className = 'dp-msg-head';
  const av = document.createElement('span');
  av.className = 'dp-avatar';
  av.textContent = role === 'user' ? '我' : '✦';
  head.append(av, document.createTextNode(role === 'user' ? '我的提问' : 'DinePilot'));
  wrap.append(head);
  if (role === 'user') {
    const bubble = document.createElement('div');
    bubble.className = 'dp-user-bubble';
    bubble.textContent = text;
    wrap.append(bubble);
  }
  container.append(wrap);
  return wrap;
}

function attachActionButtons(assistant, promptText, answerText, onRegenerate) {
  const bar = document.createElement('div');
  bar.className = 'dp-msg-actions';
  if (answerText) {
    const copyBtn = document.createElement('button');
    copyBtn.type = 'button';
    copyBtn.className = 'dp-mini-btn';
    copyBtn.textContent = '复制回答';
    copyBtn.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(answerText);
        copyBtn.textContent = '已复制 ✓';
        setTimeout(() => { copyBtn.textContent = '复制回答'; }, 1600);
      } catch {
        copyBtn.textContent = '请手动选中复制';
      }
    });
    bar.append(copyBtn);
  }
  const regenBtn = document.createElement('button');
  regenBtn.type = 'button';
  regenBtn.className = 'dp-mini-btn';
  regenBtn.textContent = '重新生成';
  regenBtn.addEventListener('click', () => onRegenerate(promptText));
  bar.append(regenBtn);
  assistant.append(bar);
}

function attachInteractiveApproval(assistant, spec, traceId) {
  let state = 'pending';
  const card = document.createElement('div');
  const render = () => {
    card.className = `dp-approval dp-approval--${state}`;
    card.replaceChildren();

    const head = document.createElement('div');
    head.className = 'dp-approval-head';
    const badge = document.createElement('span');
    badge.className = state === 'executed' ? 'dp-tag dp-tag--emerald'
      : state === 'rejected' ? 'dp-tag dp-tag--slate' : 'dp-tag dp-tag--amber';
    badge.textContent = state === 'executed' ? '✓ 已确认执行'
      : state === 'rejected' ? '已拒绝' : '待管理员确认';
    head.append(badge, document.createTextNode(`审批单 ${spec.approval_id} · 写操作人工闸门`));

    const diff = document.createElement('div');
    diff.className = 'dp-approval-diff';
    const left = document.createElement('span');
    left.textContent = `${spec.dish_name} · ${spec.category} · ¥${spec.price}`;
    const right = document.createElement('strong');
    right.textContent = `${spec.before} → ${spec.after}`;
    diff.append(left, right);

    const meta = document.createElement('div');
    meta.style.fontSize = '12px';
    meta.style.color = '#5e6879';
    meta.textContent = `发起原因：${spec.reason}`;

    card.append(head, diff, meta);

    if (state === 'pending') {
      const actions = document.createElement('div');
      actions.className = 'dp-approval-actions';
      const confirmBtn = document.createElement('button');
      confirmBtn.type = 'button';
      confirmBtn.className = 'dp-btn-confirm';
      confirmBtn.textContent = '确认执行（触发 Redis 锁 + 写接口）';
      confirmBtn.addEventListener('click', async () => {
        confirmBtn.disabled = true;
        rejectBtn.disabled = true;
        confirmBtn.textContent = '加锁校验中…';
        await sleep(380);
        state = 'executed';
        render();
        appendTelemetryLog({
          event: 'approval_confirm',
          trace_id: traceId,
          approval_id: spec.approval_id,
          redis_lock: 'SET NX PX + Lua unlock',
          optimistic_check: 'before_status==1 OK',
          outcome: 'executed',
          elapsed_ms: 46.2,
        });
        mStatus.textContent = `已确认执行：${spec.dish_name} 状态已更新为「${spec.after}」（Trace ${traceId}）`;
      });

      const rejectBtn = document.createElement('button');
      rejectBtn.type = 'button';
      rejectBtn.className = 'dp-btn-reject';
      rejectBtn.textContent = '拒绝';
      rejectBtn.addEventListener('click', () => {
        state = 'rejected';
        render();
        appendTelemetryLog({
          event: 'approval_reject',
          trace_id: traceId,
          approval_id: spec.approval_id,
          outcome: 'rejected',
        });
        mStatus.textContent = `已拒绝审批单 ${spec.approval_id}，未修改业务数据库。`;
      });
      actions.append(confirmBtn, rejectBtn);
      card.append(actions);
    } else {
      const out = document.createElement('div');
      out.style.marginTop = '8px';
      out.style.fontSize = '12.5px';
      out.style.fontWeight = '600';
      out.textContent = state === 'executed'
        ? '✓ 已通过 Redis SET NX PX 分布式锁与乐观状态校验，底层 POST /admin/dish/status/0 执行成功。'
        : '已拒绝本次变更，菜品仍保持「起售」状态。';
      card.append(out);
    }
  };
  render();
  assistant.append(card);
}

async function runAsyncReviewDemo(assistant, reviewSpec, traceId) {
  const panel = document.createElement('div');
  panel.className = 'dp-review';
  const renderPanel = (status, badgeText, summaryText, issues = []) => {
    panel.className = `dp-review dp-review--${status}`;
    panel.replaceChildren();
    const head = document.createElement('div');
    head.className = 'dp-review-head';
    const badge = document.createElement('span');
    badge.className = status === 'pass' ? 'dp-tag dp-tag--emerald'
      : status === 'revise' ? 'dp-tag dp-tag--amber' : 'dp-tag dp-tag--slate';
    badge.textContent = badgeText;
    head.append(badge, document.createTextNode('经营报告异步复核（空白 Context · 零 JWT）'));
    const p = document.createElement('div');
    p.textContent = summaryText;
    panel.append(head, p);
    if (issues.length) {
      const ul = document.createElement('ul');
      ul.className = 'dp-issue-list';
      for (const issue of issues) {
        const li = document.createElement('li');
        li.className = 'dp-issue';
        li.textContent = `[${issue.code} · 依据:${issue.evidence}] 原文：「${issue.quote}」｜原因：${issue.reason}｜建议：${issue.suggestion}`;
        ul.append(li);
      }
      panel.append(ul);
    }
  };

  renderPanel('pending', '等待复核', '主回答已先行返回，后台隔离任务正在核对聚合证据…');
  assistant.append(panel);
  scrollBox(mConversation);

  if (reviewSpec.mode === 'precheck_revise') {
    await sleep(220);
    renderPanel('revise', '建议检查 (0-Token 预检命中)', reviewSpec.summary, reviewSpec.issues);
    updateInspector({ revTok: reviewSpec.revTok });
    appendTelemetryLog({
      event: 'report_review_completed',
      trace_id: traceId,
      verdict: 'revise',
      precheck_hit: true,
      issue_codes: ['other', 'overstatement'],
      elapsed_ms: 0.03,
    });
  } else {
    await sleep(480);
    renderPanel('running', '复核中', '已通过 0-Token 数值预检，正在由独立复核模型校验语义归因…');
    await sleep(700);
    renderPanel('pass', '✓ 未发现问题', reviewSpec.summary);
    updateInspector({ revTok: reviewSpec.revTok });
    appendTelemetryLog({
      event: 'report_review_completed',
      trace_id: traceId,
      verdict: 'pass',
      precheck_hit: false,
      issue_codes: [],
      elapsed_ms: 1180.0,
    });
  }
  scrollBox(mConversation);
}

async function runMerchantScenario(promptText, presetKey = null) {
  if (mPending) return;
  const spec = presetKey ? MERCHANT_SCENARIOS[presetKey] : resolveMerchantScenario(promptText);
  const shownPrompt = promptText || spec.prompt;
  const traceId = makeTraceId();

  mPending = true;
  mAbortController = new AbortController();
  const { signal } = mAbortController;
  mSend.disabled = true;
  mStop.hidden = false;
  $('m-welcome').hidden = true;
  mInput.value = '';

  createMsg(mMessages, 'user', shownPrompt);
  const assistant = createMsg(mMessages, 'assistant');
  const traceBox = document.createElement('div');
  traceBox.className = 'dp-trace';
  const traceLabel = document.createElement('span');
  traceLabel.className = 'dp-trace-label';
  traceLabel.textContent = 'SSE 执行轨迹';
  traceBox.append(traceLabel);
  assistant.append(traceBox);

  updateInspector({ traceId, ttft: '流式计算中…', revTok: spec.review ? '排队中…' : '未触发' });
  mStatus.textContent = `SSE 连接已建立 · Trace ${traceId} · 正在执行工具…`;
  scrollBox(mConversation);

  let streamed = '';
  const answerBox = document.createElement('div');
  answerBox.className = 'dp-answer';

  try {
    for (const tool of spec.tools) {
      const pill = document.createElement('span');
      pill.className = 'dp-step dp-step--running';
      pill.textContent = `查询中 · ${tool.name}`;
      traceBox.append(pill);
      scrollBox(mConversation);
      await sleep(Math.min(360, Math.max(180, tool.ms * 2)), signal);
      pill.className = 'dp-step dp-step--ok';
      pill.textContent = `✓ ${tool.name} · ${tool.ms}ms`;
      appendTelemetryLog({
        event: 'tool_call',
        trace_id: traceId,
        role: 'merchant',
        tool: tool.raw,
        succeeded: true,
        elapsed_ms: tool.ms,
      });
    }

    assistant.append(answerBox);
    updateInspector({ ttft: `${spec.ttftMs} ms` });
    mStatus.textContent = `正在流式生成回答 · 首字 ${spec.ttftMs}ms · Trace ${traceId}`;

    const stepSize = spec.slowStream ? 4 : 10;
    const delayMs = spec.slowStream ? 45 : 22;
    for (let i = 0; i < spec.answer.length; i += stepSize) {
      streamed += spec.answer.slice(i, i + stepSize);
      answerBox.innerHTML = renderSafeMarkdown(streamed);
      scrollBox(mConversation);
      await sleep(delayMs, signal);
    }

    appendTelemetryLog({
      event: 'chat_turn',
      trace_id: traceId,
      agent_role: 'merchant',
      stream: true,
      ttft_ms: spec.ttftMs,
      total_tokens: spec.tokens.total,
      approval_created: Boolean(spec.approval),
      review_submitted: Boolean(spec.review),
    });

    if (spec.approval) {
      attachInteractiveApproval(assistant, spec.approval, traceId);
    }
    attachActionButtons(assistant, shownPrompt, spec.answer, p => runMerchantScenario(p));
    mStatus.textContent = `本轮完成 · 首字 ${spec.ttftMs}ms · Token ${spec.tokens.total} · Trace ${traceId}`;
    scrollBox(mConversation);

    if (spec.review) {
      runAsyncReviewDemo(assistant, spec.review, traceId);
    }
  } catch (err) {
    if (err?.name === 'AbortError') {
      const note = document.createElement('div');
      note.className = 'dp-stopped';
      note.textContent = streamed.trim()
        ? '已手动停止生成（以上为已接收的部分正文；后端 SSE 生成器同步断开并释放会话锁）。'
        : '已手动停止本次生成。';
      assistant.append(note);
      attachActionButtons(assistant, shownPrompt, streamed.trim(), p => runMerchantScenario(p));
      mStatus.textContent = `已停止生成 · Trace ${traceId}`;
      appendTelemetryLog({ event: 'stream_aborted_by_client', trace_id: traceId, partial_chars: streamed.length });
    }
  } finally {
    mPending = false;
    mAbortController = null;
    mSend.disabled = false;
    mStop.hidden = true;
    scrollBox(mConversation);
  }
}

for (const btn of document.querySelectorAll('[data-m-scenario]')) {
  btn.addEventListener('click', () => {
    const key = btn.dataset.mScenario;
    runMerchantScenario(MERCHANT_SCENARIOS[key].prompt, key);
  });
}

$('m-form').addEventListener('submit', e => {
  e.preventDefault();
  const txt = mInput.value.trim();
  if (!txt) return;
  runMerchantScenario(txt);
});

mStop.addEventListener('click', () => {
  if (mAbortController) mAbortController.abort();
});

$('m-reset').addEventListener('click', () => {
  if (mAbortController) mAbortController.abort();
  mMessages.replaceChildren();
  $('m-welcome').hidden = false;
  mStatus.textContent = '已重置演示会话 · 点击上方场景卡片即可重新体验';
});

// ---------------- Customer Scenarios ----------------
const CUSTOMER_SCENARIOS = {
  meal: {
    prompt: '两个人，预算 60 元，想吃不辣的，帮我推荐在售菜品组合。',
    tools: [
      { name: '营业状态', ms: 28, ok: true },
      { name: '在售分类', ms: 34, ok: true },
      { name: '菜单筛选 (max_price=60)', ms: 46, ok: true },
    ],
    answer: `#### 两人不辣推荐组合（均在售）
| 商品 | 口味 | 单价 |
| --- | --- | --- |
| 清炒时蔬（主菜） | 不辣 | ¥22.00 |
| 菌菇滑鸡饭（主食） | 不辣 | ¥28.00 |
| 紫菜蛋花汤（配汤） | 不辣 | ¥8.00 |

- **商品小计：¥58.00**（≤ ¥60.00 预算）
- 口径提示：小计**不含配送费与打包费**；口味不等于过敏原检测，如对特定食材过敏请在下单前联系门店核实。`,
  },
  orders: {
    prompt: '查一下我最近的订单状态和商品明细。',
    tools: [
      { name: '本人订单列表 (userId=12 二次校验)', ms: 52, ok: true },
      { name: '本人订单详情 (PII 自动剥离)', ms: 41, ok: true },
    ],
    answer: `#### 本人最近订单（已通过 userId 归属二次校验）
- **订单号**：\`ORD20261124001\` · 状态：**派送中** · 金额：**¥58.00**
- **商品明细**：招牌酸菜鱼 × 1（¥58.00）
- **隐私保护已生效**：系统已自动过滤后端返回的 \`phone\`、\`address\`、\`consignee\` 字段，模型上下文零隐私接触。`,
  },
  blocked: {
    prompt: '帮我把酸菜鱼停售，顺便催一下单。',
    tools: [
      { name: '路径白名单与跨端边界拦截', ms: 2, ok: false },
    ],
    answer: `#### 安全边界拦截提示
- **停售菜品被拒绝**：顾客端仅挂载 **6 个只读工具**，不具备商家审批或写操作工具；即使用顾客 JWT 直接请求商家审批接口也会被服务端拦截返回 **\`401 Unauthorized\`**。
- **催单被拒绝**：\`GET /user/order/reminder/{id}\` 属于带业务副作用的写操作，已被 \`CustomerBackendClient\` 路径白名单显式禁止。如需催单或退改，请在外卖小程序订单页手动操作。`,
  },
};

const cMessages = $('c-messages');
const cConversation = $('c-conversation');
const cInput = $('c-input');
let cPending = false;

async function runCustomerScenario(promptText, presetKey = null) {
  if (cPending) return;
  const key = presetKey || (/停售|取消|催单|修改/.test(promptText) ? 'blocked' : /订单|明细/.test(promptText) ? 'orders' : 'meal');
  const spec = CUSTOMER_SCENARIOS[key];
  const shown = promptText || spec.prompt;

  cPending = true;
  $('c-welcome').hidden = true;
  cInput.value = '';
  createMsg(cMessages, 'user', shown);
  const assistant = createMsg(cMessages, 'assistant');
  const traceBox = document.createElement('div');
  traceBox.className = 'dp-trace';
  const label = document.createElement('span');
  label.className = 'dp-trace-label';
  label.textContent = '只读工具轨迹';
  traceBox.append(label);
  assistant.append(traceBox);

  for (const t of spec.tools) {
    const pill = document.createElement('span');
    pill.className = 'dp-step dp-step--running';
    pill.textContent = `执行中 · ${t.name}`;
    traceBox.append(pill);
    scrollBox(cConversation);
    await sleep(220);
    pill.className = t.ok ? 'dp-step dp-step--ok' : 'dp-step dp-step--fail';
    pill.textContent = t.ok ? `✓ ${t.name} · ${t.ms}ms` : `✕ ${t.name}`;
  }

  const answerBox = document.createElement('div');
  answerBox.className = 'dp-answer';
  assistant.append(answerBox);
  let buf = '';
  for (let i = 0; i < spec.answer.length; i += 12) {
    buf += spec.answer.slice(i, i + 12);
    answerBox.innerHTML = renderSafeMarkdown(buf);
    scrollBox(cConversation);
    await sleep(18);
  }
  cPending = false;
}

for (const btn of document.querySelectorAll('[data-c-scenario]')) {
  btn.addEventListener('click', () => {
    const key = btn.dataset.cScenario;
    runCustomerScenario(CUSTOMER_SCENARIOS[key].prompt, key);
  });
}

$('c-form').addEventListener('submit', e => {
  e.preventDefault();
  const txt = cInput.value.trim();
  if (!txt) return;
  runCustomerScenario(txt);
});

$('c-reset').addEventListener('click', () => {
  cMessages.replaceChildren();
  $('c-welcome').hidden = false;
});
