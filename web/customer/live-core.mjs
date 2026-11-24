import { ChatError, renderMarkdown } from '../core.mjs';

export { ChatError, renderMarkdown };

const SESSION_ID_PATTERN = /^[a-zA-Z0-9_-]{1,128}$/;

export const CUSTOMER_TOOL_LABELS = {
  get_customer_shop_status: '营业状态',
  get_customer_categories: '在售分类',
  get_customer_menu: '菜单筛选',
  get_customer_setmeal_items: '套餐明细',
  get_my_orders: '本人订单列表',
  get_my_order_detail: '本人订单详情',
};

export function sanitizeUserToken(raw) {
  if (typeof raw !== 'string') return '';
  const cleaned = raw.trim().replace(/^Bearer\s+/i, '').trim();
  return /^[\x21-\x7e]{1,16384}$/.test(cleaned) ? cleaned : '';
}

export class CustomerChatClient {
  constructor({ fetchImpl = (...args) => globalThis.fetch(...args), getToken, timeoutMs = 180000 }) {
    this.fetchImpl = fetchImpl;
    this.getToken = getToken;
    this.timeoutMs = timeoutMs;
    this.busy = false;
  }

  async send(message, sessionId) {
    if (this.busy) throw new ChatError('BUSY', '已有操作进行中，请稍后再试。');
    if (typeof sessionId !== 'string' || !SESSION_ID_PATTERN.test(sessionId)) {
      throw new ChatError('INVALID_SESSION', '会话编号无效，无法发送问题。');
    }
    const text = String(message).trim();
    if (!text || text.length > 4000) throw new ChatError('INVALID_INPUT', '请输入1至4000字的问题。');
    return this.#request('/assistant-api/customer/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, message: text }),
    }, data => {
      if (
        !data || typeof data !== 'object'
        || data.agent_role !== 'customer'
        || data.session_id !== sessionId
        || typeof data.answer !== 'string'
        || !data.answer.trim()
        || data.answer.length > 200000
        || !Array.isArray(data.tool_calls)
        || data.tool_calls.length > 100
        || !data.tool_calls.every(name => typeof name === 'string' && name.length < 100)
      ) {
        throw new ChatError('BAD_RESPONSE', '用户助手返回内容不完整或会话不匹配，请稍后重试。');
      }
      return data;
    });
  }

  async clearSession(sessionId) {
    if (typeof sessionId !== 'string' || !SESSION_ID_PATTERN.test(sessionId)) {
      throw new ChatError('INVALID_SESSION', '会话编号无效，无法清空。');
    }
    return this.#request(`/assistant-api/customer/sessions/${sessionId}`, { method: 'DELETE' }, data => {
      if (data?.agent_role !== 'customer' || data.session_id !== sessionId || typeof data.cleared !== 'boolean') {
        throw new ChatError('BAD_RESPONSE', '未收到匹配的清空结果，请稍后重试。');
      }
      return data.cleared;
    });
  }

  async #request(path, options, validate) {
    if (this.busy) throw new ChatError('BUSY', '已有操作进行中，请稍后再试。');
    const token = sanitizeUserToken(this.getToken());
    if (!token) throw new ChatError('AUTH_REQUIRED', '请先粘贴有效的用户 JWT 令牌，再开始查询。');
    this.busy = true;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const response = await this.fetchImpl(path, {
        ...options,
        credentials: 'omit',
        cache: 'no-store',
        headers: { ...options.headers, Authorization: `Bearer ${token}` },
        signal: controller.signal,
      });
      if (!response.ok) {
        if (response.status === 401) throw new ChatError('AUTH_EXPIRED', '用户令牌无效或已过期，请更新后重试。');
        if (response.status === 403) throw new ChatError('FORBIDDEN', '当前用户无访问权限。');
        if (response.status === 409 && options.method === 'DELETE') throw new ChatError('SESSION_BUSY', '当前会话正在使用，请稍后再试。');
        if (response.status === 429) throw new ChatError('RATE_LIMIT', '请求较多，请稍后再试。');
        throw new ChatError('SERVICE_UNAVAILABLE', '用户助手服务暂不可用，请确认 Java 后端与 Agent 服务已启动。');
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
      if (controller.signal.aborted) throw new ChatError('TIMEOUT', '请求超时，请稍后手动重试。');
      throw new ChatError('NETWORK', '无法连接用户助手服务，请检查本地服务是否运行。');
    } finally {
      clearTimeout(timer);
      this.busy = false;
    }
  }
}
