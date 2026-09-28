# AuTo_LaTeX

一个面向数学、LaTeX 与课堂笔记的个人多智能体原型。Super 负责理解请求和调度；专家负责数学讨论、LaTeX 写作、篇章整理、TikZ、音频转写和持续笔记。OCR、记忆库、网页查询和文件编辑以工具形式提供。

## 启动

```bash
python Setup.py                 # 安装依赖并下载基础嵌入模型
python Setup.py --with-ocr      # 可选：下载 OCR 模型
python Setup.py --with-listener # 可选：下载本地语音模型
python super.py                 # Super 路由式对话
python super.py --light         # 轻量启动，不导入或加载本地重模型
python terminal.py              # 直接与指定专家对话
python terminal.py --light      # 轻量直接对话
python tests/test_runtime.py    # 离线运行时自检
```

运行前请在系统环境变量中设置 `DS_API_KEY`（或把 `settings.json` 中的 `llm.api_key_env` 改为你的变量名）。密钥不写入 JSON，也不应提交到仓库。

`--light` 仍使用远程 LLM，但不会导入或实例化 BGE、Pix2Text、Faster-Whisper，因而禁用向量记忆、OCR、文件上传识别、音频转写、Listener 与常驻 Notetaker。LaTeX、TikZ、SQLite 偏好、提醒任务、文件编辑和网页工具仍可使用。

网页顶部可切换“自动 / 讨论 / 直接 / 产出 / 探索”。自动模式先让 LLM 查看最近讨论进度并提出一个具体方式，等你确认或改选后再处理请求；探索模式逐步讨论思路，不会在你准备好前跳到复杂算法或代码。复杂精确计算由受限 Python 子进程实际运行并返回结果，不能靠模型心算。网页会话支持自定义分组、重命名和确认后删除。标准模式可拖入或选择 PDF/PNG/JPG（单个不超过 20 MB，每条消息最多 5 个）：PDF 仍按页 OCR 并标注原页码；图片在点发送后经隐藏的一轮视觉请求提取，主对话只收到图片编号和文字摘要，需要细节时可调用 `view_picture` 回看原图。图片随对话持久保存。对话可用 `/preferences` 查看或管理已确认偏好；新增、修改和停用前会要求确认。偏好保存在 `runtime_state.db`，按学习、计划、讨论等场景注入。

自然语言可创建提醒任务，系统在执行前展示待确认的时间、时区和重复规则。用 `/tasks` 查看，用 `/task complete <ID>` 停止提醒，用 `/task defer <ID> <ISO时间> [时区]` 延期。提醒由本地程序调度，只在程序运行期间触发；到期事项会在重启后提醒一次，避免漏掉后静默丢失。长讨论超过 100 轮后会滚动保存摘要，保留原始消息并支持按摘要编号回查。

## 目录与文档

根目录的 `super.py`、`terminal.py` 保持原有启动命令；实际业务代码已集中在 `src/`，离线检查在 `tests/`，示例资源在 `assets/`，分析与方案在 `reports/`。配置文件、模型、计划、缓存和数据库仍在根目录，避免迁移已有数据。

- [开发者入门：按运行顺序理解模块、函数与工作流](docs/DEVELOPER_GUIDE.md)
- [与 ChatGPT 用户端的场景差异分析](reports/project_chatgpt_analysis_2026-09-25.md)
- [产品与架构改进方案](reports/project_solution_2026-09-25.md)

其余离线检查：`python tests/test_light_mode.py`、`python tests/test_web_ui.py`、`python -m unittest tests.test_python_sandbox tests.test_personalization_modes`、`python tests/test_saver.py`。

## 配置

配置按职责拆分为 [settings.json](settings.json)、[runtime.json](runtime.json) 与 [setup.json](setup.json)：前者保存模型、路径、Listener 与提示词；`runtime.json` 保存超时、Agent 步数、工作流验收和持久化设置；`setup.json` 保存安装包、镜像和下载默认值。密钥仍只从环境变量读取。三份配置文件均不能由 Agent 的文件编辑工具读取或修改。

最常见的改动是：

- `llm`：兼容 OpenAI 的模型端点、模型名和密钥环境变量名。
- `paths`：LaTeX/TikZ/OCR/导入资料/记忆库与本地嵌入模型目录。
- `listener`：Faster-Whisper 模型、语言、实时性阈值与转写缓冲上限。
- `runtime`：Agent 步数上限、专家超时、工作流超时和常驻笔记的默认间隔。

修改配置后重启程序即可生效。请保持 JSON 合法；启动时会给出缺失字段或格式错误的明确提示。

## 当前架构

`src/Tools.py` 从函数签名生成工具 schema；`src/event_bus.py` 负责并发、慢任务、事件和危险操作确认；`src/Agent.py` 驱动模型的工具调用循环。`src/runtime/` 提供能力护照、任务和顺序工作流；`CapabilityGateway.execute()` 是实际授权边界。

Super 通过专家工具路由到 `experts.py` 中的专家。专家的工具白名单既控制可见 schema，也在执行时校验。Listener 与 notetaker 是长生命周期组件：Listener 发布转写事件，notetaker 工作流在独立的 LaTeX 校验通过后才提交转写游标。

## 工作流现状与限制

已有 `run_workflow` 可以让模型给出顺序的 `{expert, task}` 阶段，并为每一阶段收窄能力护照；因此它不是完全自由执行，权限与顺序由宿主框架约束。运行前会强制检查规划中是否有独立验收阶段；缺失时直接拒绝执行。每次工作流及其阶段状态均保存到 SQLite，便于追踪完成或失败情况。

建议下一阶段将工作流声明改为宿主校验的 JSON：每个阶段必须声明 `id`、`expert`、`goal`、`input_artifacts`、`output_artifacts`、`acceptance` 和 `retry_policy`。Super 仅能从预定义阶段模板中选择和填参；宿主负责验证上一步产物、写入阶段日志，并在失败时重试、回退或请求用户确认。这样 LLM 负责规划与内容生产，系统负责状态机、权限和验收。

## 持久化对话方案

项目使用 SQLite（默认 `runtime_state.db`）保存需要精确更新的状态，ChromaDB 只用于语义记忆检索：

1. `conversations`、`branches`、`messages` 同时保存 Super 与 terminal 的会话历史；分支切换与重启恢复使用同一存储。
2. `preferences` 只保存用户确认的稳定偏好；`tasks` 保存有截止时间、时区、重复规则和状态的提醒事项。
3. `discussion_summaries` 保存滚动讨论摘要及其覆盖消息位置；完整原始消息不裁剪，摘要引用可以通过 `view_discussion_source` 回查。
4. `workflow_runs`、`stage_runs`、`artifacts` 记录工作流和阶段结果。自动化续跑与产物版本管理尚未实现。
5. 新分支首轮会根据第一条请求从 ChromaDB 注入最多三条相关长期记忆；提到旧讨论时，Agent 可按主题搜索 SQLite 对话记录。它不会在每次启动时扫描全部历史。用户明确要求保存的信息会经过确认后写入记忆。

提醒调度器在本地进程内每 30 秒轮询。单次提醒通过事务标记为已发送；重复任务按本地时区计算下一次到期时间。
