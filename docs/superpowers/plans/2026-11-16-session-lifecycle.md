# 双端聊天会话管理 Implementation Plan

> 执行方式：确认设计与计划后，在当前会话原位实现并做一次最终只读审查；不自动提交或推送。当前Python目录非Git仓库，保留已有工作。

**Goal:** 保护使用中的会话，实现闲置回收、容量保护和本人清空API。
**Architecture:** 单进程内存SessionStore统一维护Agent、锁、使用计数与活动时间；两端Agent各持有一个Store，复用现有身份依赖。清理只作用于未被使用的条目。
**Tech Stack:** 现有Python、asyncio、FastAPI、AgentScope、pytest/httpx；无新增依赖。
**Spec:** `D:/sky-agent/docs/superpowers/specs/2026-11-16-session-lifecycle-design.md`

## Global Constraints

- 待用户确认方案后实施。默认闲置1800秒，每端每进程500个条目，均可配置；不改JWT密钥及Token有效期。
- 不启动业务服务、浏览器，不调用真实模型/Java/数据库，不改Java、部署或既有网页按钮行为。
- 使用访问时回收，不增加后台定时任务；容量满拒绝新会话，不驱逐有效会话。
- 主体只取已认证ID；清空不等于删除业务数据、日志或供应商数据。

## Review Focus

- 锁等待者已登记使用计数，不能在锁刚释放时被回收或替换。
- 取消、工厂失败和模型异常不泄漏使用计数、占位或上下文凭证。
- 资源关闭只发生在无使用者时，有时间上限且不透出内部错误。
- 相同会话ID不能跨用户或跨端删除；不存在会话不得暴露他人状态。
- 容量限制只计当前Store；不能误称全局限流或内存字节上限。

### Task 1: 有界会话存储

Files: 新增`app/memory/session_store.py`和`tests/test_session_store.py`。
接口：`SessionStore(max_entries, idle_seconds, clock, disposer)`；`async with store.use(key, factory) as agent`；`await store.clear(key) -> bool`；容量异常`SessionCapacityError`，忙碌异常`SessionBusyError`。

- [ ] 写并观察缺实现失败：虚拟时钟在TTL前复用、到边界新建；完成请求后续期；容量满已有键可用而新键失败；清空后重新创建。
- [ ] 写并观察并发失败：Event控制持有者和等待者，回收/清空均不能替换；取消等待者及持有者后能释放；工厂异常后能恢复。
- [ ] 实现统一条目和单调时钟，登记计数在第一次await之前；原子移除空闲条目后有界关闭资源。
- [ ] 验证关闭恰好一次、不关闭活跃实例、关闭失败不泄露内容且不阻塞后续查询；运行全Python套件。

### Task 2: 接入Agent和本人清空路由

Files: 修改`app/config.py`、`app/agent/merchant_agent.py`、`app/api/routes.py`、`app/models/response.py`、`.env.example`；新增API测试；更新依赖原私有缓存结构的测试。

- [ ] 先写失败测试：配置正整数约束；商家/用户会话隔离；容量异常映射503，释放ContextVar。
- [ ] 将原Agent缓存和锁迁到Store，共用执行流程，但两端Store独立。不要保留绕过容量/使用计数的生产入口；旧私有方法测试改为验证同样的可观察隔离行为。
- [ ] 先写DELETE失败测试：未认证401，路径错误422，本人删除成功/再次false，他人同名不受影响，正在使用409，无角色自选。
- [ ] 接入独立DELETE路由和`SessionClearResponse`；认证ID由现有依赖提供；不加新业务写工具。
- [ ] 运行全Python套件，确认旧商家路径、用户工具、审计与离线脚本兼容。

### Task 3: 交付与复核

Files: README、`docs/dual-agent-api.md`、会话说明及本计划执行记录。

- [ ] 更新默认值、两端/每进程范围、访问时TTL、503与409、清空的实际边界；明确页面按钮未接入。
- [ ] 统一离线检查：全Python、Node核心测试、契约导出及可选Maven离线身份7项；核对新鲜报告与未启动业务端口。
- [ ] 一次独立只读代码审查；严重/重要发现先复现失败再修复，并重跑全套。不得把离线通过写成真实模型或联调通过。

## 执行记录

用户已确认默认值与接口范围；原位实现，不提交/推送。Python目录非Git仓库，沿用已确认的原位开发方式，保留计划作为执行记录。

Task 1 complete：SessionStore及虚拟时钟/并发/取消/故障测试完成，先缺模块RED再GREEN。
Task 2 complete：两端接入Store、配置、清空API及503/409完成；14项API/配置测试先RED再GREEN。旧私有缓存测试改为验证同样的隔离行为，没有保留绕过Store的生产入口。
Final review：独立只读审查无Critical/Minor，发现退休实例在等待者帧中滞留的Important；新增回归先超时失败，再改为锁等待前关闭并清空引用。完整Python204项通过。
Scope：审查未涉及实际密钥、历史日志、部署、服务或真实联调；按本轮明确非目标保持不做，风险仍由未来联调与运行验证覆盖。
Task 3：文档已同步；统一离线检查的Python、Node18、Java身份7项与契约导出通过。新增清理中取消请求的回归后执行最终检查；业务端口无监听，未改JWT或启动服务。
最终完成：Python205/205、Node18/18、Java身份7/7；新增28项会话专项回归。无遗留严重/重要审查问题，未提交/推送；现有页面按钮后续再接。
