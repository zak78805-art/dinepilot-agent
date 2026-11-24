# Merchant Web Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 同源接入现有管理端登录和只读经营 Agent。

**Architecture:** buildless HTML/CSS/JS 位于 `D:/sky-agent/web`；独立侧栏脚本由旧入口页引入。Nginx alias 和专用代理路径隔离旧 API。

**Tech Stack:** 浏览器 ES modules、Node 内置测试、现有 Nginx/FastAPI；无新增运行依赖。

**Spec:** `D:/sky-agent/docs/superpowers/specs/2026-11-02-merchant-web-design.md`

## Global Constraints
- 不改原压缩 JS、Java 逻辑、JWT 密钥或有效期；不发布公网。
- Cookie 名 `token`；POST `/assistant-api/chat`；JSON `{session_id,message}`，最多4000字符。
- 输出不执行 HTML；模型不可信；不把 Token 写入 URL、日志或独立存储。
- 页面内存会话，发送期间防并发；失败可人工重试，不自动重发。

## Review Focus
- 恶意 HTML/链接不能执行或发出外部请求（Task 1）。
- Token 含 `=`、缺失或格式异常不能泄露或发起匿名聊天（Task 1）。
- 401/403、502、超时及非 JSON 错误给出可理解提示（Task 1/2）。
- 发送/快捷问题/新会话不能造成并发与串答（Task 1/2）。
- 旧 Vue 重绘、收起侧栏、登录页和窄屏不产生重复菜单或溢出（Task 2）。

## Task 1: Chat core
Files: `web/core.mjs`, `tests/web/core.test.mjs`.
Interfaces: `readToken(cookieText)`, `renderMarkdown(text)` -> escaped HTML; `ChatClient({fetchImpl,getToken,timeoutMs})`, `.send(message,sessionId)` -> validated response, `.busy`.
- [ ] 写测试：安全表格和代码、Cookie等号、缺凭证、错误状态、超时、并发拒绝、坏响应。
- [ ] 运行 `node --test tests/web/core.test.mjs`，确认缺实现失败。
- [ ] 实现并重复运行到全通过；运行完整 Python 测试。

## Task 2: Page and existing sidebar
Files: `web/index.html`, `web/styles.css`, `web/app.mjs`, `web/sidebar-entry.js`, `tests/web/browser-qa.cjs`.
Interfaces: consumes Task1 core; output `/assistant/` page and side entry.
- [ ] 写浏览器 QA 脚本（普通 Playwright library，不是 @playwright/test），先验证页面不存在或行为缺失。
- [ ] 实现页面、等待、错误提示、新会话、快捷问题、工具标签与侧栏兼容脚本。
- [ ] 模拟后端响应/凭证做真实浏览器交互，验证无 XSS、无重复请求、无 Token URL，桌面及手机截图。

## Task 3: Nginx integration and handoff
Files: existing nginx `conf/nginx.conf`, `html/sky/index.html`; `deploy/nginx-assistant.conf`, README.
- [ ] 用专用配置 include 加入静态alias/专用代理，原入口增加独立脚本，保留原压缩代码。
- [ ] `nginx -t`，再启动或 reload 现有 Nginx；检查配置来源，避免操作其他实例。
- [ ] 实测 `/assistant/` 与 `/assistant-api/health`，原登录页保持可用。
- [ ] 全部 Node/Python 测试和浏览器 QA；独立审查后修复重要问题；记录已验证范围。

## Execution ledger

基线：Python19项通过；两个目标目录均非 Git 仓库（Agent）或部署目录（前端），不初始化仓库、不创建 worktree、不提交。
Ruling: 保持本地目录开发，不创建 Git worktree；代价是没有自动 Git 回滚，因此只改上述目标文件并保留部署原件备份。
Pre-flight: Task1 send/result接口供Task2使用；Task2 `/assistant/` 和 `/assistant-api/` 路径与Task3一致。

Task 1: complete — 15 Node测试通过（含默认浏览器 fetch 绑定的 RED→GREEN）；Python19项通过。
Task 2: complete — 浏览器 QA RED（页面404）→GREEN；安全表格、等待、并发、错误、登录更新、移动宽度与侧栏重绘均通过。截图在 output/playwright。
Task 3: complete — nginx -t通过；已启动本机Nginx，页面/JS模块200、Agent代理健康ok、匿名聊天401、旧入口200。真实浏览器通过部署地址验证登录页2个输入框与助手页面，零脚本错误；未自动登录真实商家账号。
Ruling: 本机无 npx，使用已提供的 Playwright 库直接运行普通浏览器 QA 脚本；不安装新依赖，代价是其他机器需自行提供该测试依赖。
Final review: 独立只读审查完成（web_review），3项Important、2项Minor，无Critical。
Final: fixed 凭证切换与迟到结果隔离 — 浏览器回归测试与禁用保护的隔离测试变体RED→GREEN；清除页面记录/草稿并轮换会话，不修改后端业务数据。
Final: fixed 表格DOM放大 — pathological/cumulative表格测试RED→GREEN；限制24列、200行、累计2000格，超限纯文本。
Final: fixed 工具调用成功误示 — 浏览器检查对勾RED→GREEN；改为中性“尝试调用”，不声称成功。
Final: Ruling: 正文超时错误分支与会话保存位置文案提升为本轮需修复 — 分别影响安全重试提示和隐私认知，属于已确认错误处理/会话约束；保持误导文案的代价高于小修复。
Final: fixed 解析null和正文超时 — Node回归测试RED→GREEN；不把坏响应误报网络错误，不把正文超时误报内容错误。
Final: fixed 会话文案 — 明确刷新将开始新会话，README说明服务端现有内存上下文。
Final: no deferred minor issues. No Git branch/commit/PR created; local files and original deployment backups retained.
Final verification: Node18/18、Python19/19、完整浏览器QA（含换凭证/迟到结果/退出/折叠/登录路由）通过，部署HTML原件比对通过。真实登录后网页到模型的整轮请求仍需用户在现有登录浏览器发送验证；未读取或伪造真实登录凭证。
