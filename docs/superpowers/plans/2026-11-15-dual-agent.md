# Dual Agent Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans, inline implementation and one final read-only review.

**Goal:** 实现商家/用户双端只读Agent代码与离线验证。
**Architecture:** 独立路由、身份依赖、客户端和工具；共享现有模型创建与会话执行基础代码，实例级隔离。
**Tech Stack:** 现有FastAPI/AgentScope/httpx、SpringMVC/JUnit；无新增依赖。
**Spec:** `D:/sky-agent/docs/superpowers/specs/2026-11-15-dual-agent-design.md`

## Global Constraints
- 不启动服务，不联调，不调用真实模型/数据库，不改JWT配置。
- 不改既有脏文件（根pom、旧拦截器、OrderMapper既有改动），只新增Java用户身份控制器与独立测试。
- 无角色自选，无跨用户订单，无写工具；旧商家接口兼容。

## Review Focus
- 管理员Token不能进入用户端，反向也不行；相同数字ID/会话ID两端不共享对象。
- 用户工具不能访问admin、写接口、催单GET或额外用户ID。
- 订单列表及详情只输出本人最小字段；归属缺失或不符即失败。
- 菜单价格/状态/分类异常、金额边界及截断须显式处理。
- 异常时ContextVar归还；工具审计区分端，不能记录密钥。

## Task 1: User identity boundary
Files: Java `controller/user/CustomerAgentController.java`, `CustomerAgentControllerTest.java`; Python `clients/customer_backend_client.py`, `security/customer_auth.py`, `tests/test_customer_auth.py`.
- [x] 先写401/403、已知用户、无私密字段、跨端Token、路径拒绝测试并见RED。
- [x] 实现Java身份接口；Python `CustomerIdentity(user_id,token)`，复用Java鉴权，不本地解码JWT。
- [x] 离线pytest与指定JUnit测试；编译检查，不启动应用。

## Task 2: Customer read-only tools
Files: `tools/customer_tools.py`, `tests/test_customer_tools.py`.
- [x] 先写真实工具输入输出映射/归属/金额/状态/截断测试并见RED。
- [x] 实现6个工具：营业、分类、按分类菜单（菜品或套餐）、套餐明细、本人订单列表与详情（菜单按类型覆盖两类）。
- [x] 不注册任何写行为；单元测试全部通过。

## Task 3: Agents/routes/docs
Files: `agent/customer_agent.py`, small edits `merchant_agent.py`, new `customer_prompts.py`, routes/request model, `tests/test_dual_agent.py`, README.
- [x] 先写路由选择、身份上下文、缓存隔离、工具白名单/只读性、出错清理测试并见RED。
- [x] 商家仅抽出模型构造方法；用户Agent复用执行机制但实例、提示词、工具独立；审计加role。
- [x] 执行全Python测试、Java身份单测/编译、前端Node既有单测；只读审查并修复重要发现。

## Ledger

Baseline: Python47项与前端18项已通过。Agent目录非Git仓库，按原位开发；Java仓库有用户既有修改，保持不动。不自动提交或建立worktree，代价为依赖明确文件范围做回滚。
Pre-flight: Java身份响应user_id与Python依赖一致；CustomerBackendClient只接用户JWT；路由将认证ID与Token放入ContextVar，工具不接受user_id。
Task 1: complete — Python身份/客户端13项通过；Java用户身份4项与原商家身份3项单测通过。缺实现RED→GREEN，无服务或DB启动。
Task 2: complete — 用户工具16项通过，含跨归属/隐私裁剪/金额及菜单过滤；缺实现RED→GREEN。
Task 3: complete — 双端路由/会话/工具边界已实现，README已同步；原网页Node18项通过。
Ruling: 用户工具复用Java用户专用接口，不用管理员客户端做用户推荐；审计及缓存加role，保留商家原路由。代价：未来用户端部署需先更新Java用户身份接口。
Final review: 独立只读审查未发现Critical/Important权限问题；发现菜单异常状态/分类被静默接受的问题。
Final: Ruling: 按本轮“分类/状态异常显式处理”的验收要求修复菜单元数据问题，而非延期；代价是上游数据异常时拒绝推荐，不能静默回退。
Final: fixed 菜单分类/状态校验 — 7个错误夹具RED→GREEN；另加截断信息测试。全部Python92项通过，compileall通过。
Final verification: 身份Java单测7项（用户4+商家3）均通过，无Spring服务/DB启动；Node18项均通过。没有模型调用、服务启动、联网联调、小程序或JWT配置改动。
Deferred: 真实模型推荐质量、部署JWT配置/联调与小程序界面依用户要求未验证；既有Java脏文件不归因或修改。Python非Git目录，未执行提交或推送。
