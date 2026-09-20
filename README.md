# AuTo_LaTeX

一个面向数学、LaTeX 与课堂笔记的个人多智能体原型。Super 负责理解请求和调度；专家负责数学讨论、LaTeX 写作、篇章整理、TikZ、音频转写和持续笔记。OCR、记忆库、网页查询和文件编辑以工具形式提供。

## 启动

```bash
python Setup.py                 # 安装依赖并下载基础嵌入模型
python Setup.py --with-ocr      # 可选：下载 OCR 模型
python Setup.py --with-listener # 可选：下载本地语音模型
python super.py                 # Super 路由式对话
python terminal.py              # 直接与指定专家对话
python test_runtime.py          # 离线运行时自检
```

运行前请在系统环境变量中设置 `DS_API_KEY`（或把 `settings.json` 中的 `llm.api_key_env` 改为你的变量名）。密钥不写入 JSON，也不应提交到仓库。

## 配置

所有可调整的运行参数集中在 [settings.json](settings.json)：模型端点、输出目录、模型路径、OCR 推理提供方、Listener 参数，以及超时和常驻任务的运行时参数。`config.py` 会在启动时读取并校验该文件；业务模块不再各自保存机器路径、模型名或监听阈值。

最常见的改动是：

- `llm`：兼容 OpenAI 的模型端点、模型名和密钥环境变量名。
- `paths`：LaTeX/TikZ/OCR/记忆库与本地嵌入模型目录。
- `listener`：Faster-Whisper 模型、语言、实时性阈值与转写缓冲上限。
- `runtime`：Agent 步数上限、专家超时、工作流超时和常驻笔记的默认间隔。

修改配置后重启程序即可生效。请保持 JSON 合法；启动时会给出缺失字段或格式错误的明确提示。

## 当前架构

`Tools.py` 从函数签名生成工具 schema；`event_bus.py` 负责并发、慢任务、事件和危险操作确认；`Agent.py` 驱动模型的工具调用循环。`runtime/` 提供能力护照、任务和顺序工作流；`CapabilityGateway.execute()` 是实际授权边界。

Super 通过专家工具路由到 `experts.py` 中的专家。专家的工具白名单既控制可见 schema，也在执行时校验。Listener 与 notetaker 是长生命周期组件：Listener 发布转写事件，notetaker 工作流在独立的 LaTeX 校验通过后才提交转写游标。

## 工作流现状与限制

已有 `run_workflow` 可以让模型给出顺序的 `{expert, task}` 阶段，并为每一阶段收窄能力护照；因此它不是完全自由执行，权限与顺序由宿主框架约束。但“计划”仍由 LLM 临时生成，阶段输入只传递上一段文本结果，尚未有显式产物、验收条件、失败策略或可恢复状态。除课堂笔记工作流外，任务拆分目前是“顺序调用专家”，还不是可靠的分步交付。

建议下一阶段将工作流声明改为宿主校验的 JSON：每个阶段必须声明 `id`、`expert`、`goal`、`input_artifacts`、`output_artifacts`、`acceptance` 和 `retry_policy`。Super 仅能从预定义阶段模板中选择和填参；宿主负责验证上一步产物、写入阶段日志，并在失败时重试、回退或请求用户确认。这样 LLM 负责规划与内容生产，系统负责状态机、权限和验收。

## 持久化对话方案

建议新增 SQLite（例如 `runtime_state.db`）而不是把完整历史塞入向量库：

1. `conversations` 与 `messages` 保存会话、分支、角色、内容、时间和工具调用关联；每次追加消息或工具结果后原子提交。
2. `tasks`、`workflow_runs`、`stage_runs` 保存目标、阶段状态、输入/输出产物引用、passport 摘要、重试次数和错误；进程重启后从第一个未完成阶段恢复。
3. 将长对话压缩成可版本化的摘要，并把摘要和检索到的长期记忆作为下一轮上下文；原始消息仍保留，避免“摘要覆盖事实”。
4. 文件产物保存路径、内容哈希和版本号；校验通过后才将其标为阶段产物。副作用工具采用幂等键，防止恢复时重复写入。
5. 首先为 `terminal.py` 的分支会话实现自动保存/恢复，再接入 Super 和 `run_workflow`；最后把 Resident 的游标提交与阶段记录放进同一个事务。

这会把“可恢复会话”和“可恢复工作流”建立在同一份可审计状态上，同时保留现有 ChromaDB 只用于语义记忆检索。
