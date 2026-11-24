// Model output is untrusted: intentionally support a small, HTML-free Markdown subset.
const escapeHtml = value => String(value).replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
const inline = value => escapeHtml(value).replace(/`([^`]+)`|\*\*([^*]+)\*\*/g, (_, code, strong) => code !== undefined ? `<code>${code}</code>` : `<strong>${strong}</strong>`);
const cells = line => line.trim().replace(/^\|/, '').replace(/\|$/, '').split(/(?<!\\)\|/).map(cell => cell.trim().replace(/\\\|/g, '|'));
const tableDivider = line => line.includes('|') && cells(line).every(cell => /^:?-{3,}:?$/.test(cell));

const SESSION_ID_PATTERN = /^[a-zA-Z0-9_-]{1,128}$/;
const REVIEW_ID_PATTERN = /^[a-zA-Z0-9_-]{8,64}$/;
const APPROVAL_ID_PATTERN = /^apr_[a-zA-Z0-9_-]{8,60}$/;
const APPROVAL_STATUSES = new Set(['pending', 'executing', 'executed', 'rejected', 'expired', 'failed']);
const REVIEW_STATUSES = new Set(['pending', 'running', 'pass', 'revise', 'unavailable']);
const REVIEW_TERMINAL_STATUSES = new Set(['pass', 'revise', 'unavailable']);
const REVIEW_CODES = new Set(['status_inference', 'ranking_scope', 'unsupported_cause', 'overstatement', 'other']);

export const REVIEW_CODE_LABELS = {
  status_inference: '状态推断',
  ranking_scope: '榜单范围',
  unsupported_cause: '无证据归因',
  overstatement: '夸大推断',
  other: '其他待核对',
};

export function renderMarkdown(text) {
  const source = String(text).slice(0, 200000);
  const plainText = () => `<p>内容较长，已按纯文本显示。</p><pre class="plain-text">${escapeHtml(source)}</pre>`;
  const lines = source.replace(/\r\n?/g, '\n').split('\n');
  if (lines.length > 2000) return plainText();
  const output = [];
  let tableCells = 0;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (!line.trim()) continue;
    if (/^\s*```/.test(line)) {
      const code = [];
      while (++i < lines.length && !/^\s*```/.test(lines[i])) code.push(lines[i]);
      output.push(`<pre><code>${escapeHtml(code.join('\n'))}</code></pre>`);
    } else if (line.includes('|') && i + 1 < lines.length && tableDivider(lines[i + 1])) {
      const headers = cells(line);
      if (headers.length > 24) return plainText();
      tableCells += headers.length;
      i += 1;
      const rows = [];
      while (i + 1 < lines.length && lines[i + 1].trim() && lines[i + 1].includes('|')) {
        tableCells += headers.length;
        if (rows.length >= 200 || tableCells > 2000) return plainText();
        rows.push(cells(lines[++i]));
      }
      output.push(`<div class="table-scroll"><table><thead><tr>${headers.map(cell => `<th>${inline(cell)}</th>`).join('')}</tr></thead><tbody>${rows.map(row => `<tr>${headers.map((_, n) => `<td>${inline(row[n] || '')}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`);
    } else if (/^#{1,6}\s/.test(line)) {
      const level = Math.min(4, line.match(/^#+/)[0].length + 1);
      output.push(`<h${level}>${inline(line.replace(/^#+\s+/, ''))}</h${level}>`);
    } else if (/^\s*(?:---+|\*\*\*+)\s*$/.test(line)) {
      output.push('<hr>');
    } else if (/^\s*[-*]\s/.test(line) || /^\s*\d+[.)、]\s/.test(line)) {
      const ordered = /^\s*\d+[.)、]\s/.test(line);
      const pattern = ordered ? /^\s*\d+[.)、]\s+/ : /^\s*[-*]\s+/;
      const items = [line.replace(pattern, '')];
      while (i + 1 < lines.length && pattern.test(lines[i + 1])) items.push(lines[++i].replace(pattern, ''));
      const tag = ordered ? 'ol' : 'ul';
      output.push(`<${tag}>${items.map(item => `<li>${inline(item)}</li>`).join('')}</${tag}>`);
    } else if (/^>\s?/.test(line)) {
      output.push(`<blockquote>${inline(line.replace(/^>\s?/, ''))}</blockquote>`);
    } else {
      output.push(`<p>${inline(line)}</p>`);
    }
  }
  return output.join('\n');
}

export function readToken(cookieText) {
  const item = String(cookieText).split(';').map(part => part.trim()).find(part => part.startsWith('token='));
  if (!item) return '';
  try {
    const value = decodeURIComponent(item.slice(6));
    return /^[\x21-\x7e]{1,16384}$/.test(value) ? value : '';
  } catch { return ''; }
}

export function normalizeReportReviewRef(raw) {
  if (raw === undefined || raw === null) return null;
  if (typeof raw !== 'object' || Array.isArray(raw) || !REVIEW_STATUSES.has(raw.status)) {
    return { review_id: null, status: 'unavailable', failure: 'bad_output', poll_after_ms: null };
  }
  let reviewId = null;
  if (raw.review_id !== undefined && raw.review_id !== null) {
    if (typeof raw.review_id !== 'string' || !REVIEW_ID_PATTERN.test(raw.review_id)) {
      return { review_id: null, status: 'unavailable', failure: 'bad_output', poll_after_ms: null };
    }
    reviewId = raw.review_id;
  }
  if ((raw.status === 'pending' || raw.status === 'running') && !reviewId) {
    return { review_id: null, status: 'unavailable', failure: 'bad_output', poll_after_ms: null };
  }
  const pollAfterMs = typeof raw.poll_after_ms === 'number' && Number.isFinite(raw.poll_after_ms) && raw.poll_after_ms > 0
    ? Math.min(10000, Math.max(50, Math.round(raw.poll_after_ms)))
    : 1000;
  const failure = typeof raw.failure === 'string' && raw.failure.length <= 40 ? raw.failure : null;
  return { review_id: reviewId, status: raw.status, failure, poll_after_ms: pollAfterMs };
}

function normalizeReviewIssues(rawIssues) {
  if (!Array.isArray(rawIssues) || rawIssues.length > 8) return null;
  const issues = [];
  for (const item of rawIssues) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) return null;
    if (!REVIEW_CODES.has(item.code)) return null;
    if (typeof item.quote !== 'string' || !item.quote.trim() || item.quote.length > 500) return null;
    if (typeof item.reason !== 'string' || !item.reason.trim() || item.reason.length > 350) return null;
    if (typeof item.suggestion !== 'string' || !item.suggestion.trim() || item.suggestion.length > 500) return null;
    if (!Array.isArray(item.evidence_ids) || item.evidence_ids.length < 1 || item.evidence_ids.length > 8) return null;
    if (!item.evidence_ids.every(id => typeof id === 'string' && /^[a-z][a-z0-9_-]{0,39}$/.test(id))) return null;
    issues.push({
      code: item.code,
      quote: item.quote,
      reason: item.reason,
      suggestion: item.suggestion,
      evidence_ids: [...item.evidence_ids],
    });
  }
  return issues;
}

export function normalizeReportReviewStatus(raw, expectedSessionId, expectedReviewId) {
  const fallback = (failure = 'bad_output') => ({
    agent_role: 'merchant',
    session_id: expectedSessionId,
    review_id: expectedReviewId,
    status: 'unavailable',
    issues: [],
    failure,
    evidence_ids: [],
  });
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return fallback();
  if (raw.agent_role !== 'merchant' || raw.session_id !== expectedSessionId || raw.review_id !== expectedReviewId) {
    return fallback();
  }
  if (!REVIEW_STATUSES.has(raw.status)) return fallback();
  const issues = normalizeReviewIssues(raw.issues ?? []);
  if (!issues) return fallback();
  if (raw.status === 'pass' && issues.length > 0) return fallback();
  if (raw.status === 'revise' && issues.length === 0) return fallback();
  if ((raw.status === 'pending' || raw.status === 'running' || raw.status === 'unavailable') && issues.length > 0) {
    return fallback();
  }
  const evidenceIds = Array.isArray(raw.evidence_ids) && raw.evidence_ids.length <= 12
    && raw.evidence_ids.every(id => typeof id === 'string' && /^[a-z][a-z0-9_-]{0,39}$/.test(id))
    ? [...raw.evidence_ids]
    : [];
  const failure = typeof raw.failure === 'string' && raw.failure.length <= 40 ? raw.failure : null;
  return {
    agent_role: 'merchant',
    session_id: expectedSessionId,
    review_id: expectedReviewId,
    status: raw.status,
    issues,
    failure,
    evidence_ids: evidenceIds,
  };
}

export function describeReportReview(review) {
  const status = review?.status;
  if (status === 'pending') {
    return {
      badge: '等待复核',
      tone: 'pending',
      summary: '报告已先行返回，后台正在排队核对文字结论与本轮聚合证据…',
    };
  }
  if (status === 'running') {
    return {
      badge: '复核中',
      tone: 'running',
      summary: '报告已先行返回，正在核对文字结论与本轮聚合证据…',
    };
  }
  if (status === 'pass') {
    return {
      badge: '未发现问题',
      tone: 'pass',
      summary: '基于本轮聚合工具证据核对未指出问题（不代表人工终审或数字全量核验）。',
    };
  }
  if (status === 'revise') {
    const count = Array.isArray(review.issues) ? review.issues.length : 0;
    return {
      badge: '建议检查',
      tone: 'revise',
      summary: `发现 ${count} 处待确认表述，原报告已保留，请对照以下提示人工核对：`,
    };
  }
  const failureMap = {
    timeout: '复核超时，原报告已保留。请结合统计口径自行核对，不视为复核通过。',
    queue_full: '当前复核队列较忙，本次未启动复核，原报告已保留，不视为复核通过。',
    not_found: '复核任务已过期或服务已重启，原报告已保留，不视为复核通过。',
    bad_output: '复核输出未通过校验，原报告已保留，不视为复核通过。',
    bad_input: '本轮报告长度或证据超出复核范围，原报告已保留，不视为复核通过。',
  };
  return {
    badge: '未完成复核',
    tone: 'unavailable',
    summary: failureMap[review?.failure] || '复核暂不可用，原报告已保留。请结合统计口径自行核对，不视为复核通过。',
  };
}

export function normalizeApprovalTicket(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  if (typeof raw.approval_id !== 'string' || !APPROVAL_ID_PATTERN.test(raw.approval_id)) return null;
  if (!APPROVAL_STATUSES.has(raw.status)) return null;
  if (typeof raw.summary !== 'string' || !raw.summary.trim() || raw.summary.length > 300) return null;
  const target = raw.target && typeof raw.target === 'object' ? raw.target : null;
  const before = raw.before_state && typeof raw.before_state === 'object' ? raw.before_state : null;
  const after = raw.after_state && typeof raw.after_state === 'object' ? raw.after_state : null;
  if (!target || typeof target.dish_name !== 'string' || !before || typeof before.status_name !== 'string' || !after || typeof after.status_name !== 'string') {
    return null;
  }
  return {
    agent_role: 'merchant',
    approval_id: raw.approval_id,
    session_id: typeof raw.session_id === 'string' ? raw.session_id : '',
    action: raw.action === 'change_dish_status' ? 'change_dish_status' : 'change_dish_status',
    status: raw.status,
    summary: raw.summary,
    target: {
      dish_id: typeof target.dish_id === 'number' ? target.dish_id : null,
      dish_name: target.dish_name,
      category: typeof target.category === 'string' ? target.category : null,
      price: typeof target.price === 'number' && Number.isFinite(target.price) ? target.price : null,
    },
    before_state: { status: before.status, status_name: before.status_name },
    after_state: { status: after.status, status_name: after.status_name },
    reason: typeof raw.reason === 'string' && raw.reason.trim() ? raw.reason : null,
    created_at: typeof raw.created_at === 'string' ? raw.created_at : '',
    expires_at: typeof raw.expires_at === 'string' ? raw.expires_at : '',
    decided_at: typeof raw.decided_at === 'string' ? raw.decided_at : null,
    decision_reason: typeof raw.decision_reason === 'string' ? raw.decision_reason : null,
    execution_message: typeof raw.execution_message === 'string' ? raw.execution_message : null,
  };
}

export function describeApprovalStatus(status) {
  const map = {
    pending: { badge: '待人工确认', tone: 'pending' },
    executing: { badge: '执行中…', tone: 'pending' },
    executed: { badge: '已确认执行', tone: 'executed' },
    rejected: { badge: '已拒绝', tone: 'rejected' },
    expired: { badge: '已过期', tone: 'expired' },
    failed: { badge: '执行未完成', tone: 'failed' },
  };
  return map[status] || { badge: '状态未知', tone: 'expired' };
}

export function consumeSseBuffer(buffer, onFrame) {
  const normalized = String(buffer).replace(/\r\n/g, '\n');
  const parts = normalized.split('\n\n');
  const remainder = parts.pop() ?? '';
  for (const rawBlock of parts) {
    if (!rawBlock.trim()) continue;
    let eventName = 'message';
    const dataLines = [];
    for (const line of rawBlock.split('\n')) {
      if (line.startsWith('event:')) eventName = line.slice(6).trim() || 'message';
      else if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart());
    }
    if (!dataLines.length) continue;
    try {
      const payload = JSON.parse(dataLines.join('\n'));
      onFrame(eventName, payload);
    } catch {
      // Ignore malformed SSE JSON chunks safely.
    }
  }
  return remainder;
}

export class ChatError extends Error {
  constructor(code, message) { super(message); this.code = code; }
}

export class ChatClient {
  constructor({ fetchImpl = (...args) => globalThis.fetch(...args), getToken, timeoutMs = 180000 }) {
    this.fetchImpl = fetchImpl;
    this.getToken = getToken;
    this.timeoutMs = timeoutMs;
    this.busy = false;
  }

  #validateChatPayload(data, sessionId) {
    if (!data || typeof data !== 'object' || data.session_id !== sessionId || typeof data.answer !== 'string' || !data.answer.trim() || data.answer.length > 200000 || !Array.isArray(data.tool_calls) || data.tool_calls.length > 100 || !data.tool_calls.every(name => typeof name === 'string' && name.length < 100)) {
      throw new ChatError('BAD_RESPONSE', '服务返回内容不完整或会话不匹配，请稍后重试。');
    }
    if (data.report_review === undefined && data.approval === undefined) return data;
    const normalized = { ...data };
    if (data.report_review !== undefined) normalized.report_review = normalizeReportReviewRef(data.report_review);
    if (data.approval !== undefined) normalized.approval = normalizeApprovalTicket(data.approval);
    return normalized;
  }

  async send(message, sessionId) {
    if (this.busy) throw new ChatError('BUSY', '已有操作进行中，请稍后再试。');
    if (typeof sessionId !== 'string' || !SESSION_ID_PATTERN.test(sessionId)) {
      throw new ChatError('INVALID_SESSION', '会话编号无效，无法发送问题。');
    }
    const text = String(message).trim();
    if (!text || text.length > 4000) throw new ChatError('INVALID_INPUT', '请输入1至4000字的问题。');
    return this.#request('/assistant-api/chat', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, message: text }),
    }, data => this.#validateChatPayload(data, sessionId));
  }

  async sendStream(message, sessionId, { onEvent = () => {}, signal } = {}) {
    if (this.busy) throw new ChatError('BUSY', '已有操作进行中，请稍后再试。');
    if (typeof sessionId !== 'string' || !SESSION_ID_PATTERN.test(sessionId)) {
      throw new ChatError('INVALID_SESSION', '会话编号无效，无法发送问题。');
    }
    const text = String(message).trim();
    if (!text || text.length > 4000) throw new ChatError('INVALID_INPUT', '请输入1至4000字的问题。');
    if (signal?.aborted) throw new ChatError('ABORTED', '已停止本次生成。');
    const token = this.getToken();
    if (!token) throw new ChatError('AUTH_REQUIRED', '请先在同一浏览器的管理端登录，再返回此页。');

    this.busy = true;
    const controller = new AbortController();
    let timedOut = false;
    const onExternalAbort = () => controller.abort();
    if (signal) signal.addEventListener('abort', onExternalAbort, { once: true });
    const timer = setTimeout(() => { timedOut = true; controller.abort(); }, this.timeoutMs);
    try {
      const response = await this.fetchImpl('/assistant-api/chat', {
        method: 'POST',
        credentials: 'same-origin',
        cache: 'no-store',
        headers: {
          'Content-Type': 'application/json',
          Accept: 'text/event-stream, application/json',
          token,
        },
        body: JSON.stringify({ session_id: sessionId, message: text }),
        signal: controller.signal,
      });
      if (!response.ok) {
        if (response.status === 401) throw new ChatError('AUTH_EXPIRED', '登录已失效，请返回管理端重新登录。');
        if (response.status === 403) throw new ChatError('FORBIDDEN', '当前账号无访问权限或已停用，请联系管理员。');
        if (response.status === 429) throw new ChatError('RATE_LIMIT', '请求较多，请稍后再试。');
        throw new ChatError('SERVICE_UNAVAILABLE', '助手服务暂不可用，请确认 Java 后端与 Agent 服务都已启动，稍后再试。');
      }

      const contentType = String(response.headers?.get?.('content-type') || '').toLowerCase();
      if (!contentType.includes('text/event-stream') || typeof response.body?.getReader !== 'function') {
        let data;
        try { data = await response.json(); }
        catch (error) {
          if (controller.signal.aborted) throw error;
          throw new ChatError('BAD_RESPONSE', '服务返回了无效内容，请稍后重试。');
        }
        return this.#validateChatPayload(data, sessionId);
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder('utf-8');
      let buffer = '';
      let donePayload = null;
      let streamError = null;

      const handleFrame = (eventName, payload) => {
        if (eventName === 'done') {
          donePayload = payload;
          onEvent(eventName, payload);
        } else if (eventName === 'error') {
          streamError = payload;
          onEvent(eventName, payload);
        } else {
          onEvent(eventName, payload);
        }
      };

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        buffer = consumeSseBuffer(buffer, handleFrame);
      }
      buffer += decoder.decode();
      if (buffer.trim()) consumeSseBuffer(buffer + '\n\n', handleFrame);

      if (streamError) {
        const code = streamError.code === 'SESSION_CAPACITY' ? 'SERVICE_UNAVAILABLE' : 'SERVICE_UNAVAILABLE';
        const msg = typeof streamError.message === 'string' && streamError.message.length <= 200
          ? streamError.message
          : '助手服务暂不可用，请稍后再试。';
        throw new ChatError(code, msg);
      }
      if (!donePayload) {
        throw new ChatError('BAD_RESPONSE', '流式响应意外中断，请稍后重试。');
      }
      return this.#validateChatPayload(donePayload, sessionId);
    } catch (error) {
      if (error instanceof ChatError) throw error;
      if (signal?.aborted) throw new ChatError('ABORTED', '已停止本次生成。');
      if (timedOut || controller.signal.aborted) throw new ChatError('TIMEOUT', '请求超时。服务端可能仍在处理，请稍后手动重试。');
      throw new ChatError('NETWORK', '无法连接助手服务，请检查网络和本地服务是否运行。');
    } finally {
      clearTimeout(timer);
      if (signal) signal.removeEventListener('abort', onExternalAbort);
      this.busy = false;
    }
  }

  async decideApproval(approvalId, decision, { reason, timeoutMs = 15000 } = {}) {
    if (typeof approvalId !== 'string' || !APPROVAL_ID_PATTERN.test(approvalId)) {
      throw new ChatError('INVALID_APPROVAL', '审批单编号无效。');
    }
    if (decision !== 'confirm' && decision !== 'reject') {
      throw new ChatError('INVALID_INPUT', '审批操作仅支持 confirm 或 reject。');
    }
    const token = this.getToken();
    if (!token) throw new ChatError('AUTH_REQUIRED', '请先在同一浏览器的管理端登录，再返回此页。');
    const body = typeof reason === 'string' && reason.trim() ? { reason: reason.trim().slice(0, 200) } : {};
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await this.fetchImpl(`/assistant-api/merchant/approvals/${approvalId}/${decision}`, {
        method: 'POST',
        credentials: 'same-origin',
        cache: 'no-store',
        headers: { 'Content-Type': 'application/json', token },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      if (!response.ok) {
        if (response.status === 401) throw new ChatError('AUTH_EXPIRED', '登录已失效，请返回管理端重新登录。');
        if (response.status === 403) throw new ChatError('FORBIDDEN', '当前账号无审批执行权限。');
        if (response.status === 404) throw new ChatError('APPROVAL_NOT_FOUND', '审批单不存在或无权操作。');
        if (response.status === 410) throw new ChatError('APPROVAL_EXPIRED', '审批单已过期，请重新发起修改请求。');
        if (response.status === 409) throw new ChatError('APPROVAL_CONFLICT', '审批单已处理或目标菜品状态已变更，不可重复执行。');
        throw new ChatError('SERVICE_UNAVAILABLE', '审批执行失败，请检查业务后端状态后重试。');
      }
      let data;
      try { data = await response.json(); }
      catch (error) {
        if (controller.signal.aborted) throw error;
        throw new ChatError('BAD_RESPONSE', '未收到有效的审批结果，请刷新核对菜品状态。');
      }
      const ticket = normalizeApprovalTicket(data);
      if (!ticket || ticket.approval_id !== approvalId) {
        throw new ChatError('BAD_RESPONSE', '审批返回编号不匹配，请核对菜品状态。');
      }
      return ticket;
    } catch (error) {
      if (error instanceof ChatError) throw error;
      if (controller.signal.aborted) throw new ChatError('TIMEOUT', '审批请求超时，请到菜品管理核对当前状态。');
      throw new ChatError('NETWORK', '无法连接助手服务，请检查网络和本地服务是否运行。');
    } finally {
      clearTimeout(timer);
    }
  }

  async clearSession(sessionId) {
    if (typeof sessionId !== 'string' || !SESSION_ID_PATTERN.test(sessionId)) {
      throw new ChatError('INVALID_SESSION', '会话编号无效，无法清空。');
    }
    return this.#request(`/assistant-api/merchant/sessions/${sessionId}`, { method: 'DELETE' }, data => {
      if (data?.agent_role !== 'merchant' || data.session_id !== sessionId || typeof data.cleared !== 'boolean') {
        throw new ChatError('BAD_RESPONSE', '未收到匹配的清空结果，请稍后重试。');
      }
      return data.cleared;
    });
  }

  async getReportReview(sessionId, reviewId, { signal, timeoutMs = 10000 } = {}) {
    if (typeof sessionId !== 'string' || !SESSION_ID_PATTERN.test(sessionId)) {
      throw new ChatError('INVALID_SESSION', '会话编号无效，无法查询复核状态。');
    }
    if (typeof reviewId !== 'string' || !REVIEW_ID_PATTERN.test(reviewId)) {
      throw new ChatError('INVALID_REVIEW', '复核编号无效，无法查询复核状态。');
    }
    if (signal?.aborted) throw new ChatError('ABORTED', '复核轮询已停止。');
    const token = this.getToken();
    if (!token) throw new ChatError('AUTH_REQUIRED', '请先在同一浏览器的管理端登录，再返回此页。');

    const controller = new AbortController();
    let timedOut = false;
    const onExternalAbort = () => controller.abort();
    if (signal) signal.addEventListener('abort', onExternalAbort, { once: true });
    const timer = setTimeout(() => { timedOut = true; controller.abort(); }, timeoutMs);
    try {
      const response = await this.fetchImpl(`/assistant-api/merchant/sessions/${sessionId}/report-reviews/${reviewId}`, {
        method: 'GET',
        credentials: 'same-origin',
        cache: 'no-store',
        headers: { token },
        signal: controller.signal,
      });
      if (response.status === 404) {
        return {
          agent_role: 'merchant',
          session_id: sessionId,
          review_id: reviewId,
          status: 'unavailable',
          issues: [],
          failure: 'not_found',
          evidence_ids: [],
        };
      }
      if (!response.ok) {
        if (response.status === 401) throw new ChatError('AUTH_EXPIRED', '登录已失效，请返回管理端重新登录。');
        if (response.status === 403) throw new ChatError('FORBIDDEN', '当前账号无访问权限或已停用，请联系管理员。');
        if (response.status === 429) throw new ChatError('RATE_LIMIT', '请求较多，请稍后再试。');
        throw new ChatError('SERVICE_UNAVAILABLE', '复核服务暂不可用，请稍后核对。');
      }
      let data;
      try { data = await response.json(); }
      catch (error) {
        if (signal?.aborted) throw new ChatError('ABORTED', '复核轮询已停止。');
        if (timedOut) throw new ChatError('TIMEOUT', '复核状态查询超时。');
        return {
          agent_role: 'merchant',
          session_id: sessionId,
          review_id: reviewId,
          status: 'unavailable',
          issues: [],
          failure: 'bad_output',
          evidence_ids: [],
        };
      }
      return normalizeReportReviewStatus(data, sessionId, reviewId);
    } catch (error) {
      if (error instanceof ChatError) throw error;
      if (signal?.aborted) throw new ChatError('ABORTED', '复核轮询已停止。');
      if (timedOut || controller.signal.aborted) throw new ChatError('TIMEOUT', '复核状态查询超时。');
      throw new ChatError('NETWORK', '无法连接复核服务，请检查网络和本地服务是否运行。');
    } finally {
      clearTimeout(timer);
      if (signal) signal.removeEventListener('abort', onExternalAbort);
    }
  }

  async #request(path, options, validate) {
    if (this.busy) throw new ChatError('BUSY', '已有操作进行中，请稍后再试。');
    const token = this.getToken();
    if (!token) throw new ChatError('AUTH_REQUIRED', '请先在同一浏览器的管理端登录，再返回此页。');
    this.busy = true;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const response = await this.fetchImpl(path, {
        ...options, credentials: 'same-origin', cache: 'no-store',
        headers: { ...options.headers, token }, signal: controller.signal,
      });
      if (!response.ok) {
        if (response.status === 401) throw new ChatError('AUTH_EXPIRED', '登录已失效，请返回管理端重新登录。');
        if (response.status === 403) throw new ChatError('FORBIDDEN', '当前账号无访问权限或已停用，请联系管理员。');
        if (response.status === 409 && options.method === 'DELETE') throw new ChatError('SESSION_BUSY', '当前会话正在使用，请稍后重试。');
        if (response.status === 429) throw new ChatError('RATE_LIMIT', '请求较多，请稍后再试。');
        throw new ChatError('SERVICE_UNAVAILABLE', '助手服务暂不可用，请确认 Java 后端与 Agent 服务都已启动，稍后再试。');
      }
      let data;
      try { data = await response.json(); }
      catch (error) {
        if (controller.signal.aborted) throw error;
        throw new ChatError('BAD_RESPONSE', '服务返回了无效内容，请稍后重试。');
      }
      return validate(data);
    } catch (error) {
      if (error instanceof ChatError) throw error;
      if (controller.signal.aborted) throw new ChatError('TIMEOUT', '请求超时。服务端可能仍在处理，请稍后手动重试。');
      throw new ChatError('NETWORK', '无法连接助手服务，请检查网络和本地服务是否运行。');
    } finally {
      clearTimeout(timer);
      this.busy = false;
    }
  }
}

function defaultSleep(ms, signal) {
  return new Promise(resolve => {
    if (signal?.aborted || ms <= 0) { resolve(); return; }
    const onAbort = () => { clearTimeout(timer); resolve(); };
    const timer = setTimeout(() => {
      if (signal) signal.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    if (signal) signal.addEventListener('abort', onAbort, { once: true });
  });
}

export async function pollReportReview({
  client,
  sessionId,
  reviewId,
  intervalMs = 1000,
  deadlineMs = 20000,
  maxTransientErrors = 2,
  onUpdate = () => {},
  signal,
  now = () => Date.now(),
  sleep = defaultSleep,
}) {
  const deadline = now() + Math.max(1, deadlineMs);
  let transientErrors = 0;

  while (!signal?.aborted) {
    const remaining = deadline - now();
    if (remaining <= 0) {
      const timedOutState = {
        agent_role: 'merchant',
        session_id: sessionId,
        review_id: reviewId,
        status: 'unavailable',
        issues: [],
        failure: 'timeout',
        evidence_ids: [],
      };
      onUpdate(timedOutState);
      return timedOutState;
    }

    try {
      const result = await client.getReportReview(sessionId, reviewId, {
        signal,
        timeoutMs: Math.min(10000, Math.max(20, remaining)),
      });
      if (signal?.aborted) return null;
      transientErrors = 0;
      onUpdate(result);
      if (REVIEW_TERMINAL_STATUSES.has(result.status)) return result;
    } catch (error) {
      if (signal?.aborted || error?.code === 'ABORTED') return null;
      if (['AUTH_EXPIRED', 'FORBIDDEN', 'AUTH_REQUIRED'].includes(error?.code)) {
        const failed = {
          agent_role: 'merchant',
          session_id: sessionId,
          review_id: reviewId,
          status: 'unavailable',
          issues: [],
          failure: 'unavailable',
          evidence_ids: [],
          authError: error,
        };
        onUpdate(failed);
        return failed;
      }
      transientErrors += 1;
      if (transientErrors >= maxTransientErrors || deadline - now() <= 0) {
        const failed = {
          agent_role: 'merchant',
          session_id: sessionId,
          review_id: reviewId,
          status: 'unavailable',
          issues: [],
          failure: error?.code === 'TIMEOUT' ? 'timeout' : 'unavailable',
          evidence_ids: [],
        };
        onUpdate(failed);
        return failed;
      }
    }

    const waitMs = Math.min(intervalMs, Math.max(0, deadline - now()));
    if (waitMs <= 0) {
      if (signal?.aborted) return null;
      const timedOutState = {
        agent_role: 'merchant',
        session_id: sessionId,
        review_id: reviewId,
        status: 'unavailable',
        issues: [],
        failure: 'timeout',
        evidence_ids: [],
      };
      onUpdate(timedOutState);
      return timedOutState;
    }
    await sleep(waitMs, signal);
  }
  return null;
}
