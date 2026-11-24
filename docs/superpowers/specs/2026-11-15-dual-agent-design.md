# 双端 Agent（用户已确认范围）

单个Python服务内分离MerchantAgent和CustomerAgent，共享模型配置，不共享工具、身份或会话实例。商家9个只读工具与旧接口保持兼容。用户端第一版为只读菜单分类/菜品/套餐/套餐内容/营业状态/本人订单查询，按真实价格、口味信息协助推荐。推荐仅为候选商品金额，不承诺含运费、打包费或库存，不臆测过敏原。

新增 `/api/agent/merchant/chat`（原 `/api/agent/chat` 保留）和 `/api/agent/customer/chat`。路由分别执行管理员/用户鉴权，不信任请求中的role或user_id。Java增加受用户JWT拦截器保护的 `/user/agent/identity`，返回数据库核对后的user_id。Python转发用户令牌到既有 `authentication` 请求头，不保存或解码签名密钥。

用户只读客户端限制可访问的GET路径，不准访问admin路径或GET催单等写行为。本人订单由Java用户接口校验归属，Python额外核对返回userId；输出最小字段，排除电话、地址等不必要隐私。会话按端和认证ID隔离，审计加端类型。用户工具不注册购物车、下单、支付、催单、取消。

不做：小程序界面、服务启动、网络/模型/数据库联调、JWT密钥或有效期修改、生产部署、持久化。只运行离线Python/Java单元测试与编译。
