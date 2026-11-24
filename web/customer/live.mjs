import {
  CUSTOMER_TOOL_LABELS,
  CustomerChatClient,
  renderMarkdown,
  sanitizeUserToken,
} from './live-core.mjs';

const $ = id => document.getElementById(id);
const tokenInput = $('user-token');
const question = $('question');
const messages = $('messages');
const status = $('status');
const quickButtons = [...document.querySelectorAll('.quick-question')];

let activeToken = '';
let rejectedToken = '';
let sessionId = `customer_${crypto.randomUUID().replace(/-/g, '')}`;
let pending = false;
let credentialGeneration = 0;
let conversationGeneration = 0;
let timer;

const client = new CustomerChatClient({ getToken: () => activeToken });

function startConversation() {
  conversationGeneration++;
  sessionId = `customer_${crypto.randomUUID().replace(/-/g, '')}`;
  messages.replaceChildren();
  $('welcome').hidden = false;
  question.value = '';
  $('char-count').textContent = '0';
}

function syncControls() {
  const blocked = !activeToken || activeToken === rejectedToken;
  $('auth-notice').hidden = !blocked;
  $('connection-status').textContent = blocked ? '等待用户输入令牌' : '用户令牌已加载（仅页面内存）';
  $('send').disabled = pending || blocked;
  $('new-session').disabled = pending || blocked;
  question.disabled = pending;
  for (const button of quickButtons) button.disabled = pending;
  if (!activeToken) {
    $('auth-text').textContent = '请先输入用户端 JWT 令牌（不复用管理端 Cookie，刷新页面后自动清空）。';
  }
}

function applyToken(rawValue) {
  const trimmed = String(rawValue ?? '').trim();
  const next = sanitizeUserToken(rawValue);
  if (trimmed && !next) {
    $('auth-notice').hidden = false;
    $('auth-text').textContent = '令牌格式无效（包含空格、换行或非法字符），请粘贴合法的用户 JWT 字符串。';
    status.textContent = '令牌格式无效，请检查后重新粘贴。';
    return;
  }
  if (next === activeToken) {
    syncControls();
    return;
  }
  activeToken = next;
  rejectedToken = '';
  credentialGeneration++;
  startConversation();
  status.textContent = activeToken
    ? (pending ? '用户令牌已变更，旧请求结果不会显示。' : '用户令牌已更新，已开始新会话。')
    : '已清除用户令牌。';
  syncControls();
}

function markRejected(error) {
  if (!['AUTH_EXPIRED', 'FORBIDDEN', 'AUTH_REQUIRED'].includes(error.code)) return false;
  rejectedToken = activeToken;
  $('auth-text').textContent = error.message;
  return true;
}

function scrollLatest() {
  $('conversation').scrollTop = $('conversation').scrollHeight;
}

function addMessage(role, text = '') {
  const article = document.createElement('article');
  article.className = `message ${role}-message`;
  const heading = document.createElement('div');
  heading.className = 'message-heading';
  const avatar = document.createElement('span');
  avatar.className = 'message-avatar';
  avatar.setAttribute('aria-hidden', 'true');
  avatar.textContent = role === 'user' ? '我' : '✦';
  heading.append(avatar, document.createTextNode(role === 'user' ? '我的问题' : '点餐助手'));
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

async function submit(event) {
  event.preventDefault();
  if (pending) return;
  const text = question.value.trim();
  if (!text || text.length > 4000) {
    status.textContent = '请输入1至4000字的问题。';
    question.focus();
    return;
  }
  syncControls();
  if ($('send').disabled) {
    status.textContent = '请先配置有效的用户令牌。';
    return;
  }
  pending = true;
  const reqCredGen = credentialGeneration;
  const reqConvGen = conversationGeneration;
  const reqSessionId = sessionId;
  syncControls();
  $('welcome').hidden = true;
  addMessage('user', text);
  const assistant = addMessage('assistant');
  const placeholder = document.createElement('div');
  placeholder.className = 'pending-text';
  placeholder.textContent = '正在查询在售菜单或本人订单…';
  assistant.append(placeholder);
  const started = Date.now();
  status.classList.add('loading');
  const tick = () => {
    if (reqCredGen === credentialGeneration) {
      status.textContent = `正在查询 · 已等待 ${Math.floor((Date.now() - started) / 1000)} 秒`;
    }
  };
  tick();
  timer = setInterval(tick, 1000);
  scrollLatest();
  try {
    const result = await client.send(text, reqSessionId);
    if (reqCredGen !== credentialGeneration || reqConvGen !== conversationGeneration) return;
    placeholder.remove();
    const answer = document.createElement('div');
    answer.className = 'answer-body';
    answer.innerHTML = renderMarkdown(result.answer);
    assistant.append(answer);
    if (result.tool_calls.length) {
      const tools = document.createElement('div');
      tools.className = 'tools';
      const caption = document.createElement('span');
      caption.className = 'tools-caption';
      caption.textContent = '本轮调用工具';
      tools.append(caption);
      for (const name of result.tool_calls) {
        const chip = document.createElement('span');
        chip.className = 'tool-chip';
        chip.textContent = CUSTOMER_TOOL_LABELS[name] || name;
        chip.title = name;
        tools.append(chip);
      }
      assistant.append(tools);
    }
    question.value = '';
    $('char-count').textContent = '0';
    status.textContent = `本轮回答完成 · ${(Date.now() - started) / 1000 | 0} 秒`;
  } catch (error) {
    if (reqCredGen !== credentialGeneration || reqConvGen !== conversationGeneration) return;
    placeholder.remove();
    const notice = document.createElement('div');
    notice.className = 'message-error';
    notice.textContent = error.message || '本次查询失败，请稍后重试。';
    markRejected(error);
    assistant.append(notice);
    status.textContent = '本次未完成，问题已保留，请处理后手动重试。';
  } finally {
    clearInterval(timer);
    status.classList.remove('loading');
    pending = false;
    syncControls();
    if (reqCredGen !== credentialGeneration) {
      status.textContent = '用户令牌已变更，已开始新会话；旧请求结果未显示。';
    }
    scrollLatest();
  }
}

$('apply-token').addEventListener('click', () => applyToken(tokenInput.value));
$('clear-token').addEventListener('click', () => {
  tokenInput.value = '';
  applyToken('');
});
$('chat-form').addEventListener('submit', submit);
question.addEventListener('input', () => { $('char-count').textContent = String(question.value.length); });
question.addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault();
    if (!$('send').disabled) $('chat-form').requestSubmit();
  }
});
for (const button of quickButtons) {
  button.addEventListener('click', () => {
    if (pending) return;
    question.value = button.dataset.question;
    question.dispatchEvent(new Event('input'));
    question.focus();
  });
}
$('new-session').addEventListener('click', async () => {
  if (pending) return;
  syncControls();
  if ($('new-session').disabled) return;
  const gen = credentialGeneration;
  if (!window.confirm('清空当前对话和未发送草稿，并开始新会话？')) return;
  if (gen !== credentialGeneration) return;
  pending = true;
  syncControls();
  status.textContent = '正在清空当前对话…';
  try {
    const cleared = await client.clearSession(sessionId);
    if (gen !== credentialGeneration) return;
    startConversation();
    status.textContent = cleared ? '已清空当前对话并开始新会话。' : '旧会话不存在或已过期，已开始新会话。';
  } catch (error) {
    if (gen !== credentialGeneration) return;
    markRejected(error);
    status.textContent = `未确认清空，对话与草稿已保留。${error.message || '请稍后重试。'}`;
  } finally {
    pending = false;
    syncControls();
    if (gen !== credentialGeneration) status.textContent = '用户令牌已变更，已开始新会话；旧请求结果未显示。';
    else if (!$('send').disabled) question.focus();
  }
});

syncControls();
