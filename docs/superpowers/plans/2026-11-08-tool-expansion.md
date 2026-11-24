# 商家 Agent 只读工具扩充 — 实施方案

**目标：** 把只读工具从 4 个扩到 9 个，覆盖「订单状态/列表/详情、菜品目录、用户增长」，全部走已验证返回 200 的后端接口。

**范围：** 只做只读。不加任何写操作、不加审批流（那是下一阶段）。

---

## 现状核对（已实测）

### 可直接用的后端接口

| 接口 | 实测 | 用途 |
| --- | --- | --- |
| `GET /admin/order/statistics` | 200 `{toBeConfirmed:0,confirmed:0,deliveryInProgress:0}` | 待接单/待派送/派送中计数 |
| `GET /admin/order/conditionSearch?page&pageSize&status&number&phone` | 200，`status=6` 返回 2 条 | 按状态列订单 |
| `GET /admin/order/details/{id}` | 200，含 `cancelReason`/`cancelTime`/`orderDetailList` | 订单详情 |
| `GET /admin/report/userStatistics?begin&end` | 200，`totalUserList`/`newUserList` | 新增与累计用户 |
| `GET /admin/dish/page?page&pageSize&status` | 200，`status=0` → 1 条停售，`status=1` → 23 条在售 | 菜品目录与停售情况 |
| `GET /admin/category/list?type=1` | 200，12 个分类 | 分类名映射 |

### 一个必须先修的 Java bug

`GET /admin/order/conditionSearch?beginTime=...&endTime=...` → **500**

```
### Error querying database.
Cause: java.lang.IllegalArgumentException: invalid comparison: java.time.LocalDateTime and java.lang.String
```

根因在 `D:\后端初始工程\sky-server\src\main\resources\mapper\OrderMapper.xml:70-71`：

```xml
<if test="beginTime!=null and beginTime!=''">and order_time&gt;=#{beginTime}</if>
<if test="endTime!=null and endTime!=''">and order_time&lt;=#{endTime}</if>
```

`beginTime` 是 `LocalDateTime`，`!=''` 会让 OGNL 拿 `LocalDateTime` 和 `String` 比较，直接抛异常。前端 `shopTable.fe534d8f.js` 传了 `beginTime`，所以**管理端订单页按时间筛选本来就是坏的**，只是没人报。

---

## Task 1：修订单日期筛选（前置）

- 文件：`D:\后端初始工程\sky-server\src\main\resources\mapper\OrderMapper.xml`
- 改法：两个 `<if>` 去掉 `and beginTime!=''` / `and endTime!=''`，只留 `!=null`
- 重编译：`D:\后端初始工程\mvnw.cmd -q -DskipTests package`（maven 3.9.9 已缓存在 `~/.m2/wrapper/dists`），然后重启 8080
- 验收：`GET /api/order/conditionSearch?page=1&pageSize=10&beginTime=2026-08-04 00:00:00&endTime=2026-08-04 23:59:59` → 200 且 total=4
- 回退：改回 XML 并重新打包即可；jar 原件先备份

**如果不想动 Java**，`get_order_list` 就只支持 `status`/`number`/`phone`，不支持日期。功能少一截。

## Task 2：新增 5 个只读工具

### `app/tools/order_tools.py`（扩充）

| 函数 | 后端 | 返回要点 |
| --- | --- | --- |
| `get_order_status_overview()` | `/admin/order/statistics` | `{to_be_confirmed, confirmed, delivery_in_progress}` |
| `get_order_list(status, limit, begin, end)` | `/admin/order/conditionSearch` | `orders: [{id, number, status, status_name, amount, order_time, phone, address}]`，`total` |
| `get_order_detail(order_id)` | `/admin/order/details/{id}` | 订单字段 + `cancel_reason`/`cancel_time` + `dishes: [{name, number, amount}]` |

`get_order_detail` 目前是 `raise NotImplementedError("等待接入 Java 订单详情接口")`（`order_tools.py:34-36`），本任务把它实现掉。

状态映射用后端常量（`sky-pojo/.../Orders.java`）：
`1 待付款 / 2 待接单 / 3 已接单 / 4 派送中 / 5 已完成 / 6 已取消`（7 退款未用）

### `app/tools/dish_tools.py`（扩充）

| 函数 | 后端 | 返回要点 |
| --- | --- | --- |
| `get_dish_catalog(status, limit)` | `/admin/dish/page` | `{total, dishes: [{id, name, category, price, status, status_name}]}`，`status=None` 表示全部 |

`status=0` 即在售/停售筛选，实测能查到 `炝炒圆白菜` 这一条停售菜。

### `app/tools/user_tools.py`（新建）

| 函数 | 后端 | 返回要点 |
| --- | --- | --- |
| `get_user_statistics(begin, end)` | `/admin/report/userStatistics` | `daily: [{date, total_users, new_users}]` + `statistics_scope` |

沿用 `report_utils.date_range` 的校验（最多 366 天、不允许未来）。

### 统一约束（沿用现有风格）

- 每个工具都返回 `statistics_scope`，写清时间字段、状态过滤、口径与限制
- 不编造、不推断「取消原因」「经营好坏」，与现有 4 个工具一致
- 全部 `is_read_only=True`

## Task 3：注册到 Agent + 提示词

- `app/agent/merchant_agent.py`：加 5 个 `_tracked_*` 包装（复用 `_run_tool`，自动进审计日志与 `tool_calls`），在 `Toolkit(tools=[...])` 里注册成 `FunctionTool(..., is_read_only=True)`
- `app/agent/prompts.py`：新增规则
  - 问「哪些单没处理/被取消了」→ `get_order_status_overview` + `get_order_list`
  - 问「某个订单怎么回事」→ `get_order_detail`
  - 问「有多少菜在卖/哪些停售了」→ `get_dish_catalog`
  - 问「来了多少新客」→ `get_user_statistics`
  - 明确：订单状态是**查询时快照**，不代表历史流转；取消原因只有 `cancelReason` 有值时才可说
  - 保持现有第 22-23 条（本阶段无修改工具）

## Task 4：测试

- 新建 `tests/test_tools_stage2.py`：mock `SkyBackendClient.get`，断言
  - 请求路径与 params 正确
  - `status` 名称映射、`limit` 边界、`order_id` 必填
  - 后端返回结构异常时抛 `RuntimeError`
  - 日期越界仍被 `date_range` 拒绝
- 断言 `Toolkit` 里 9 个工具**全部** `is_read_only=True`，且没有任何写工具混入
- 现有 19 项必须继续通过

## Task 5：真实验证

用 `_work/browser_verify.mjs` 的 CDP 流程 + HTTP 直连，各问一遍：

| 问题 | 期望命中 |
| --- | --- |
| 现在有哪些订单还没处理？ | `get_order_status_overview` |
| 列出已取消的订单 | `get_order_list` |
| 订单 7 为什么取消的？ | `get_order_detail` |
| 现在有多少菜在卖，哪些停售了？ | `get_dish_catalog` |
| 8月1日到8月7日来了多少新用户？ | `get_user_statistics` |

核对点：工具名出现在 `tool_calls`、`logs/tool_calls.log` 有对应行、回答里的数字与后端直查一致。

## 验收标准

1. `pytest tests` 全绿（19 + 新增）
2. `node --test tests/web/core.test.mjs` 仍 18 passed
3. 5 个新问题都能命中预期工具且数字与后端一致
4. 没有引入任何写接口，`is_read_only` 全 True
5. `logs/tool_calls.log` 能看到新工具的 operator/arguments/elapsed_ms

## 不做的事

- 不加写操作（停售、接单、取消）
- 不接审批流
- 不接 Redis 会话持久化
- 不改 JWT、不改前端页面

---

## 执行结果（2026-11-08 已完成）

全部 5 个工具已实现并验证，测试 43 → 47 passed。

### 实施中发现的额外缺陷（已修）

1. **订单号 vs 主键**：用户口语里的「订单号」是 
umber（如 1785838137567），但 /admin/order/details/{id} 要的是数据库主键。首轮验证问「订单号 1785838137567 点了哪些菜」时，模型把订单号当 order_id 传，返回「订单不存在」。
   - 修法：get_order_detail(order_id=None, order_number=None) 增加订单号入口，先用 conditionSearch?number= 反查主键，命中多条则拒绝；返回值加 statistics_scope.resolved_from 标明来源。
   - get_order_list 同时开放 
umber / phone 模糊筛选（后端本就是 like）。
   - 提示词第 14 条明确「用户说的订单号要传 order_number」。

2. **limit 上限不一致**：get_order_list 上限 50、get_dish_catalog 上限 100，导致模型反复试 limit=100 失败重试（审计日志有 3 次 success=False）。统一为 100。

### 最终验证数据（与后端直查逐项一致）

| 问题 | 命中工具 | 结果 |
| --- | --- | --- |
| 待接单/已接单/派送中多少 | get_order_status_overview | 0 / 0 / 0（三状态快照） |
| 8月4日哪些订单被取消 | get_order_list(status=6, 8/4) | 2 笔：1785838137567「骑手不足无法配送」、1785836932573「用户取消」 |
| 8月4日共几笔订单 | get_order_list(8/4) | 4 笔，2 已完成 / 2 已取消，金额 25+79+79+79 |
| 订单号 1785838137567 点了哪些菜 | get_order_detail(order_number=...) | 蒜蓉娃娃菜 ×1 = 18 元，订单 25 元（含打包费） |
| 几个菜品停售 | get_dish_catalog(status=0) | 1 个：炝炒圆白菜 / 新鲜时蔬 / 18 元 |
| 8/1–8/7 新增用户 | get_user_statistics | 1 人（8月4日注册） |

### 浏览器端到端（CDP 驱动真实 Chrome）

真实登录 → /assistant/ → 连问 4 题，全部命中预期工具，errors: []，工具 chip 逐轮累加，截图 _work/assistant-stage2-desktop.png。