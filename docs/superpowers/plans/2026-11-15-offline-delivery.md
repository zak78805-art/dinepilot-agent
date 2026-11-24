# 双端离线验证与接口交付计划

范围：承接用户“继续添加不需要启动或联调的部分”。不改变双端业务权限，不增加写操作、前端或持久化。

1. 离线测试防线：pytest默认阻止真实TCP连接/DNS；补充httpx MockTransport用户请求头、路径和错误结果测试，以及双端并发上下文隔离测试。
2. 接口契约：从FastAPI和实际工具注册表导出OpenAPI、商家/用户工具JSON；无需模型密钥。独立提取toolkit工厂，不改变现有工具注册。
3. 统一验证脚本：只执行Python测试、Node核心测试、契约导出；可选离线Maven身份单测（-o），生成JSON汇总与JUnit结果，任一失败返回非零。禁止启动服务或执行任意传入命令。
4. 文档：双端接口请求/错误/身份边界、未实现范围、离线运行命令与联调区分。

验证：新增测试先RED再实现；运行全Python、Node18项和可选Java身份7项。不跑浏览器（测试浏览器会启动临时HTTP服务），不调用真实模型/业务API/数据库。

记录：直接沿用现有本地目录，不提交或推送；仅修改工具工厂提取、测试、离线脚本和文档。测试/导出中不得输出.env、Token、数据库配置或模型密钥。

执行记录：统一检查脚本、实际契约导出、接口文档、MockTransport及三并发身份测试已完成。Windows socketpair例外仅包围标准库内部构造，不开放localhost。

独立只读审查：两项Important已修复。DNS入口补全（getaddrinfo/gethostbyname/gethostbyname_ex/gethostbyaddr/getnameinfo），回归测试先确认非原生解析器再调用，RED→GREEN无真实DNS。Java检查不再只看退出码，要求商家和用户两份Surefire XML本轮新生成、执行测试非零且无跳过/失败；缺失、旧报告、跳过和失败夹具均RED→GREEN。

附带修正：仅使用已安装Maven或项目精确版本缓存，不触发wrapper下载；相邻版本3.9.99不可代替3.9.9。无服务、浏览器、真实模型或DB联调；不改JWT。接口Schema导出不等于模型选工具或回答质量评测。

最终统一验证通过：Python115项、Node18项、Java身份7项，新鲜Surefire报告和JUnit已核对。导出商家9个/用户6个工具的真实Schema。结果位于output/offline及output/contracts（Git忽略）；无未处理的审查发现。
