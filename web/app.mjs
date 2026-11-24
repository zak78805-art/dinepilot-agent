import {
  ChatClient,
  REVIEW_CODE_LABELS,
  describeApprovalStatus,
  describeReportReview,
  pollReportReview,
  readToken,
  renderMarkdown,
} from './core.mjs';

const $ = id => document.getElementById(id);
const question = $('question');
const messages = $('messages');
const status = $('status');
const quickButtons = [...document.querySelectorAll('.quick-question')];
const client = new ChatClient({ getToken: () => readToken(document.cookie) });
const activeReviewPollers = new Set();
let sessionId = crypto.randomUUID();
let rejectedToken = '';
let pending = false;
let timer;
let activeStreamController = null;
let observedToken = readToken(document.cookie);
let credentialGeneration = 0;
let conversationGeneration = 0;

function stopReviewPollers() {
  for (const controller of activeReviewPollers) controller.abort();
  activeReviewPollers.clear();
}

function startConversation() {
  stopReviewPollers();
  conversationGeneration++;
  sessionId = crypto.randomUUID();
  messages.replaceChildren();
  $('welcome').hidden = false;
  question.value = '';
  $('char-count').textContent = '0';
}

function syncControls() {
  const token = readToken(document.cookie);
  if (token !== observedToken) {
    observedToken = token;
    credentialGeneration++;
    rejectedToken = '';
    startConversation();
    status.textContent = pending ? '登录凭证已变化，旧请求结束后可继续；旧请求结果不会显示。' : '登录凭证已变化，已开始新会话。';
  }
  const blocked = !token || token === rejectedToken;
  $('auth-notice').hidden = !blocked;
  $('connection-status').textContent = blocked ? '请先登录管理端' : '已读取管理端登录凭证';
  $('send').disabled = pending || blocked;
  $('new-session').disabled = pending || blocked;
  const stopBtn = $('stop-stream');
  if (stopBtn) {
    stopBtn.hidden = !pending;
    stopBtn.disabled = !pending;
  }
  question.disabled = pending;
  for (const button of quickButtons) button.disabled = pending;
  if (!token) $('auth-text').textContent = '请先在同一浏览器的管理端登录，再返回此页。';
}

function markRejectedCredential(error) {
  if (!['AUTH_EXPIRED', 'FORBIDDEN', 'AUTH_REQUIRED'].includes(error.code)) return false;
  rejectedToken = observedToken;
  $('auth-text').textContent = error.message;
  return true;
}

function scrollLatest() { $('conversation').scrollTop = $('conversation').scrollHeight; }

function addMessage(role, text = '') {
  const article = document.createElement('article');
  article.className = `message ${role}-message`;
  const heading = document.createElement('div');
  heading.className = 'message-heading';
  const avatar = document.createElement('span');
  avatar.className = 'message-avatar';
  avatar.setAttribute('aria-hidden', 'true');
  avatar.textContent = role === 'user' ? '我' : '✦';
  heading.append(avatar, document.createTextNode(role === 'user' ? '我的问题' : '经营助手'));
  article.append(heading);
  if (role === 'user') {
    const bubble = document.createElement('div');
    bubble.className = 'user-bubble';
    bubble.textContent = text;
    article.append(bubble);
  }
  messages.append(article);
  return article;
}

// Render reviewer output strictly via textContent; never overwrite the original report.
function renderReportReview(container, review) {
  const view = describeReportReview(review);
  container.className = `report-review report-review--${view.tone}`;
  container.setAttribute('aria-live', 'polite');
  container.replaceChildren();

  const header = document.createElement('div');
  header.className = 'report-review-header';
  const badge = document.createElement('span');
  badge.className = `report-review-badge report-review-badge--${view.tone}`;
  badge.textContent = view.badge;
  const label = document.createElement('span');
  label.className = 'report-review-label';
  label.textContent = '经营报告复核';
  header.append(badge, label);

  const summary = document.createElement('p');
  summary.className = 'report-review-summary';
  summary.textContent = view.summary;
  container.append(header, summary);

  if (review.status === 'revise' && Array.isArray(review.issues) && review.issues.length) {
    const list = document.createElement('ul');
    list.className = 'report-review-issues';
    for (const issue of review.issues) {
      const item = document.createElement('li');
      item.className = 'report-review-issue';

      const meta = document.createElement('div');
      meta.className = 'report-review-issue-meta';
      const codeTag = document.createElement('span');
      codeTag.className = 'report-review-issue-code';
      codeTag.textContent = REVIEW_CODE_LABELS[issue.code] || issue.code;
      meta.append(codeTag);
      if (Array.isArray(issue.evidence_ids) && issue.evidence_ids.length) {
        const evidenceTag = document.createElement('span');
        evidenceTag.className = 'report-review-issue-evidence';
        evidenceTag.textContent = `依据：${issue.evidence_ids.join('、')}`;
        meta.append(evidenceTag);
      }

      const quote = document.createElement('div');
      quote.className = 'report-review-issue-quote';
      quote.textContent = `原文：「${issue.quote}」`;

      const reason = document.createElement('div');
      reason.className = 'report-review-issue-reason';
      reason.textContent = `原因：${issue.reason}`;

      const suggestion = document.createElement('div');
      suggestion.className = 'report-review-issue-suggestion';
      suggestion.textContent = `建议：${issue.suggestion}`;

      item.append(meta, quote, reason, suggestion);
      list.append(item);
    }
    container.append(list);
  }
}

function attachReportReview(assistant, reviewRef, { msgSessionId, msgConversationGen, msgCredentialGen }) {
  if (!reviewRef) return;
  const panel = document.createElement('section');
  panel.setAttribute('aria-label', '经营报告异步复核状态');
  renderReportReview(panel, reviewRef);
  assistant.append(panel);

  if (!reviewRef.review_id || !['pending', 'running'].includes(reviewRef.status)) return;

  const controller = new AbortController();
  activeReviewPollers.add(controller);
  const pollConfig = globalThis.__SKY_REVIEW_POLL_CONFIG__ || {};
  const intervalMs = typeof pollConfig.intervalMs === 'number' ? pollConfig.intervalMs : (reviewRef.poll_after_ms || 1000);
  const deadlineMs = typeof pollConfig.deadlineMs === 'number' ? pollConfig.deadlineMs : 30000;

  pollReportReview({
    client,
    sessionId: msgSessionId,
    reviewId: reviewRef.review_id,
    intervalMs,
    deadlineMs,
    signal: controller.signal,
    onUpdate(update) {
      syncControls();
      if (controller.signal.aborted || msgConversationGen !== conversationGeneration || msgCredentialGen !== credentialGeneration) {
        return;
      }
      if (update.authError) {
        markRejectedCredential(update.authError);
        syncControls();
      }
      renderReportReview(panel, update);
      if (['pass', 'revise', 'unavailable'].includes(update.status)
          && !pending
          && typeof status.textContent === 'string'
          && status.textContent.includes('经营报告后台复核中')) {
        const badge = describeReportReview(update).badge;
        status.textContent = status.textContent.replace('经营报告后台复核中', `经营报告复核：${badge}`);
      }
    },
  }).finally(() => {
    activeReviewPollers.delete(controller);
  });
}

// Render HITL write-approval confirmation card strictly via textContent.
function attachApprovalCard(assistant, initialTicket, { msgConversationGen, msgCredentialGen }) {
  if (!initialTicket) return;
  const card = document.createElement('section');
  card.setAttribute('aria-label', '写操作人工审批确认');
  let ticket = initialTicket;
  let deciding = false;
  let errorMessage = '';

  function render() {
    const view = describeApprovalStatus(deciding ? 'executing' : ticket.status);
    card.className = `approval-card approval-card--${view.tone}`;
    card.replaceChildren();

    const header = document.createElement('div');
    header.className = 'approval-card-header';
    const badge = document.createElement('span');
    badge.className = `approval-badge approval-badge--${view.tone}`;
    badge.textContent = view.badge;
    const label = document.createElement('span');
    label.className = 'approval-label';
    label.textContent = '写操作审批单 · 需管理员确认';
    header.append(badge, label);

    const summary = document.createElement('p');
    summary.className = 'approval-summary';
    summary.textContent = ticket.summary;

    const diff = document.createElement('div');
    diff.className = 'approval-diff';
    const dishInfo = document.createElement('span');
    dishInfo.className = 'approval-dish';
    const metaParts = [ticket.target.dish_name];
    if (ticket.target.category) metaParts.push(ticket.target.category);
    if (typeof ticket.target.price === 'number') metaParts.push(`¥${ticket.target.price}`);
    dishInfo.textContent = metaParts.join(' · ');

    const transition = document.createElement('span');
    transition.className = 'approval-transition';
    transition.textContent = `${ticket.before_state.status_name} → ${ticket.after_state.status_name}`;
    diff.append(dishInfo, transition);

    card.append(header, summary, diff);

    if (ticket.reason) {
      const reasonNode = document.createElement('p');
      reasonNode.className = 'approval-meta';
      reasonNode.textContent = `发起原因：${ticket.reason}`;
      card.append(reasonNode);
    }

    if (ticket.execution_message) {
      const outcomeNode = document.createElement('p');
      outcomeNode.className = 'approval-outcome';
      outcomeNode.textContent = ticket.execution_message;
      card.append(outcomeNode);
    }

    if (errorMessage) {
      const errNode = document.createElement('p');
      errNode.className = 'approval-error';
      errNode.textContent = errorMessage;
      card.append(errNode);
    }

    if (ticket.status === 'pending') {
      const actions = document.createElement('div');
      actions.className = 'approval-actions';

      const confirmBtn = document.createElement('button');
      confirmBtn.type = 'button';
      confirmBtn.className = 'approval-btn approval-btn--confirm';
      confirmBtn.textContent = deciding ? '正在执行…' : '确认执行';
      confirmBtn.disabled = deciding;
      confirmBtn.addEventListener('click', () => handleDecision('confirm'));

      const rejectBtn = document.createElement('button');
      rejectBtn.type = 'button';
      rejectBtn.className = 'approval-btn approval-btn--reject';
      rejectBtn.textContent = '拒绝';
      rejectBtn.disabled = deciding;
      rejectBtn.addEventListener('click', () => handleDecision('reject'));

      actions.append(confirmBtn, rejectBtn);
      card.append(actions);
    }
  }

  async function handleDecision(decision) {
    if (deciding || ticket.status !== 'pending') return;
    syncControls();
    if (msgConversationGen !== conversationGeneration || msgCredentialGen !== credentialGeneration) return;
    deciding = true;
    errorMessage = '';
    render();
    try {
      const updated = await client.decideApproval(ticket.approval_id, decision);
      syncControls();
      if (msgConversationGen !== conversationGeneration || msgCredentialGen !== credentialGeneration) return;
      ticket = updated;
      status.textContent = updated.execution_message || (decision === 'confirm' ? '已确认执行菜品状态修改。' : '已拒绝本次修改。');
    } catch (error) {
      syncControls();
      if (msgConversationGen !== conversationGeneration || msgCredentialGen !== credentialGeneration) return;
      markRejectedCredential(error);
      if (error.code === 'APPROVAL_EXPIRED') {
        ticket = { ...ticket, status: 'expired', execution_message: error.message };
      } else if (error.code === 'APPROVAL_CONFLICT') {
        ticket = { ...ticket, status: 'failed', execution_message: error.message };
      } else {
        errorMessage = error.message || '审批操作未完成，请稍后重试。';
      }
      status.textContent = error.message || '审批操作未完成。';
    } finally {
      deciding = false;
      if (msgConversationGen === conversationGeneration && msgCredentialGen === credentialGeneration) {
        render();
      }
    }
  }

  render();
  assistant.append(card);
}

const toolLabels = {
  get_today_sales: '今日经营数据',
  get_turnover_statistics: '营业额趋势',
  get_order_statistics: '订单统计',
  get_top_dishes: '商品销量排名',
  get_order_status_overview: '订单流转概览',
  get_order_list: '订单列表',
  get_order_detail: '订单详情',
  get_dish_catalog: '菜品目录',
  get_user_statistics: '用户增长统计',
  get_period_comparison_report: '并行环比经营诊断',
  request_dish_status_change: '菜品状态审批',
};

function attachMessageActions(assistant, { promptText, answerText }) {
  const bar = document.createElement('div');
  bar.className = 'message-actions';

  if (answerText) {
    const copyBtn = document.createElement('button');
    copyBtn.type = 'button';
    copyBtn.className = 'msg-action-btn';
    copyBtn.textContent = '复制回答';
    copyBtn.addEventListener('click', async () => {
      try {
        if (globalThis.navigator?.clipboard?.writeText) {
          await globalThis.navigator.clipboard.writeText(answerText);
          copyBtn.textContent = '已复制 ✓';
          setTimeout(() => { copyBtn.textContent = '复制回答'; }, 1800);
        } else {
          status.textContent = '当前浏览器不支持剪贴板写入，请手动选中复制。';
        }
      } catch {
        status.textContent = '复制失败，请手动选中回答文本复制。';
      }
    });
    bar.append(copyBtn);
  }

  if (promptText) {
    const regenBtn = document.createElement('button');
    regenBtn.type = 'button';
    regenBtn.className = 'msg-action-btn';
    regenBtn.textContent = '重新生成';
    regenBtn.addEventListener('click', () => {
      if (pending) return;
      runSubmit(promptText);
    });
    bar.append(regenBtn);
  }

  if (bar.children?.length || bar.childNodes?.length) assistant.append(bar);
}

async function submit(event) {
  event.preventDefault();
  await runSubmit(question.value);
}

async function runSubmit(rawText) {
  if (pending) return;
  const text = String(rawText ?? '').trim();
  if (!text || text.length > 4000) { status.textContent = '请输入1至4000字的问题。'; question.focus(); return; }
  syncControls();
  if ($('send').disabled) { status.textContent = '请先完成管理端登录。'; return; }
  pending = true;
  const streamController = new AbortController();
  activeStreamController = streamController;
  const requestGeneration = credentialGeneration;
  const requestConversationGen = conversationGeneration;
  const requestSessionId = sessionId;
  syncControls();
  $('welcome').hidden = true;
  addMessage('user', text);
  const assistant = addMessage('assistant');
  const placeholder = document.createElement('div');
  placeholder.className = 'pending-text';
  placeholder.textContent = '正在分析，完成后会显示本轮调用的工具…';
  assistant.append(placeholder);
  const started = Date.now();
  status.classList.add('loading');
  const tick = () => {
    syncControls();
    if (requestGeneration === credentialGeneration) status.textContent = `正在分析 · 已等待 ${Math.floor((Date.now() - started) / 1000)} 秒`;
  };
  tick();
  timer = setInterval(tick, 1000);
  scrollLatest();
  let traceBox = null;
  const traceSteps = new Map();
  let streamAnswerBox = null;
  let streamBuffer = '';

  function ensureTraceBox() {
    if (!traceBox) {
      traceBox = document.createElement('div');
      traceBox.className = 'tool-trace';
      traceBox.setAttribute('aria-label', '工具执行轨迹');
      const title = document.createElement('span');
      title.className = 'tool-trace-title';
      title.textContent = '执行轨迹';
      traceBox.append(title);
      assistant.append(traceBox);
    }
    return traceBox;
  }

  function ensureAnswerBox() {
    if (!streamAnswerBox) {
      placeholder.remove();
      streamAnswerBox = document.createElement('div');
      streamAnswerBox.className = 'answer-body';
      assistant.append(streamAnswerBox);
    }
    return streamAnswerBox;
  }

  try {
    const result = await client.sendStream(text, requestSessionId, {
      signal: streamController.signal,
      onEvent(eventName, payload) {
        if (requestGeneration !== credentialGeneration || requestConversationGen !== conversationGeneration) return;
        if (eventName === 'tool_start' && payload?.tool_name) {
          const box = ensureTraceBox();
          const step = document.createElement('span');
          step.className = 'trace-step trace-step--running';
          const label = toolLabels[payload.tool_name] || payload.tool_name;
          step.textContent = `查询中 · ${label}`;
          box.append(step);
          traceSteps.set(payload.tool_call_id || `${payload.tool_name}_${traceSteps.size}`, { step, label });
          status.textContent = `正在调用工具：${label}…`;
          scrollLatest();
        } else if (eventName === 'tool_end' && payload) {
          const entry = traceSteps.get(payload.tool_call_id);
          const label = entry?.label || toolLabels[payload.tool_name] || payload.tool_name || '工具';
          const ms = typeof payload.elapsed_ms === 'number' ? ` · ${Math.round(payload.elapsed_ms)}ms` : '';
          if (entry) {
            entry.step.className = payload.succeeded ? 'trace-step trace-step--ok' : 'trace-step trace-step--fail';
            entry.step.textContent = payload.succeeded ? `✓ ${label}${ms}` : `! ${label} 失败${ms}`;
          }
          scrollLatest();
        } else if (eventName === 'delta' && typeof payload?.text === 'string' && payload.text) {
          streamBuffer += payload.text;
          const answerBox = ensureAnswerBox();
          answerBox.innerHTML = renderMarkdown(streamBuffer); // Only escaped HTML-free Markdown from core.mjs.
          scrollLatest();
        }
      },
    });
    syncControls();
    if (requestGeneration !== credentialGeneration || requestConversationGen !== conversationGeneration) return;
    placeholder.remove();
    const answer = ensureAnswerBox();
    answer.innerHTML = renderMarkdown(result.answer); // Only escaped HTML-free Markdown from core.mjs.
    if (result.tool_calls.length) {
      const tools = document.createElement('div');
      tools.className = 'tools';
      const caption = document.createElement('span');
      caption.className = 'tools-caption';
      caption.textContent = '本轮尝试调用（不代表全部成功）';
      tools.append(caption);
      for (const name of result.tool_calls) {
        const chip = document.createElement('span');
        chip.className = 'tool-chip';
        chip.textContent = toolLabels[name] || name;
        chip.title = name;
        tools.append(chip);
      }
      assistant.append(tools);
    }
    attachApprovalCard(assistant, result.approval_required ? result.approval : null, {
      msgConversationGen: requestConversationGen,
      msgCredentialGen: requestGeneration,
    });
    attachReportReview(assistant, result.report_review, {
      msgSessionId: requestSessionId,
      msgConversationGen: requestConversationGen,
      msgCredentialGen: requestGeneration,
    });
    attachMessageActions(assistant, { promptText: text, answerText: result.answer });
    question.value = '';
    $('char-count').textContent = '0';
    const reviewHint = result.report_review && ['pending', 'running'].includes(result.report_review.status)
      ? ' · 经营报告后台复核中'
      : '';
    const ttftHint = typeof result.metrics?.ttft_ms === 'number'
      ? ` · 首字 ${(result.metrics.ttft_ms / 1000).toFixed(1)} 秒`
      : '';
    const traceHint = typeof result.trace_id === 'string' && result.trace_id
      ? ` · Trace ${result.trace_id}`
      : '';
    status.textContent = `本轮回答完成${ttftHint} · ${(Date.now() - started) / 1000 | 0} 秒${reviewHint}${traceHint}`;
  } catch (error) {
    syncControls();
    if (requestGeneration !== credentialGeneration || requestConversationGen !== conversationGeneration) return;
    placeholder.remove();
    if (error?.code === 'ABORTED') {
      const stoppedNote = document.createElement('p');
      stoppedNote.className = 'stream-stopped-note';
      stoppedNote.textContent = streamBuffer.trim()
        ? '已手动停止生成（以上为已接收的部分内容）。'
        : '已手动停止本次生成。';
      assistant.append(stoppedNote);
      attachMessageActions(assistant, { promptText: text, answerText: streamBuffer.trim() });
      status.textContent = '已停止本次生成，你可以修改问题或点击重新生成。';
    } else {
      const notice = document.createElement('div');
      notice.className = 'message-error';
      notice.textContent = error.message || '本次查询失败，请稍后重试。';
      if (markRejectedCredential(error)) {
        const link = document.createElement('a');
        link.href = '/#/login';
        link.textContent = '返回管理端';
        notice.append(link);
      }
      assistant.append(notice);
      attachMessageActions(assistant, { promptText: text, answerText: '' });
      status.textContent = '本次未完成，问题已保留。请根据提示处理后手动重试。';
    }
  } finally {
    clearInterval(timer);
    if (activeStreamController === streamController) activeStreamController = null;
    status.classList.remove('loading');
    pending = false;
    syncControls();
    if (requestGeneration !== credentialGeneration) status.textContent = '登录凭证已变化，已开始新会话；旧请求结果未显示。';
    scrollLatest();
  }
}

const stopButton = $('stop-stream');
if (stopButton) {
  stopButton.addEventListener('click', () => {
    if (activeStreamController) activeStreamController.abort();
  });
}

$('chat-form').addEventListener('submit', submit);
question.addEventListener('input', () => { $('char-count').textContent = String(question.value.length); });
question.addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault();
    if (!$('send').disabled) $('chat-form').requestSubmit();
  }
});
for (const button of quickButtons) button.addEventListener('click', () => {
  if (pending) return;
  question.value = button.dataset.question;
  question.dispatchEvent(new Event('input'));
  question.focus();
});
$('new-session').addEventListener('click', async () => {
  if (pending) return;
  syncControls();
  if ($('new-session').disabled) return;
  const generation = credentialGeneration;
  if (!window.confirm('清空当前对话和未发送的草稿，并开始新会话？')) return;
  // Login can change in another tab while the confirmation is open.
  syncControls();
  if (generation !== credentialGeneration) return;
  pending = true;
  syncControls();
  status.textContent = '正在清空当前对话…';
  try {
    const cleared = await client.clearSession(sessionId);
    syncControls();
    if (generation !== credentialGeneration) return;
    startConversation();
    status.textContent = cleared ? '已清空当前对话并开始新会话。' : '旧会话不存在或已过期，已开始新会话。';
  } catch (error) {
    syncControls();
    if (generation !== credentialGeneration) return;
    markRejectedCredential(error);
    status.textContent = `未确认清空，页面上的对话和草稿已保留。${error.message || '请稍后重试。'}`;
  } finally {
    pending = false;
    syncControls();
    if (generation !== credentialGeneration) status.textContent = '登录凭证已变化，已开始新会话；旧请求结果未显示。';
    else if (!$('send').disabled) question.focus();
  }
});
window.addEventListener('focus', syncControls);
window.addEventListener('pagehide', stopReviewPollers);
document.addEventListener('visibilitychange', () => { if (!document.hidden) syncControls(); });
syncControls();
