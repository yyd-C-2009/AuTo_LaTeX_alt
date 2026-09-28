# 任务从输入到完成：当前执行流程

本文以“用户在 Super 中提交一个需要多个专家协作的任务”为主线，说明当前代码实际如何执行。普通工具调用、专家调用和显式工作流共用同一套 Agent、Bus 与权限机制；同一种循环只解释一次。

> 例：用户输入“识别这份讲义，核对数学内容，并整理成可编译的 LaTeX 笔记”。

## 0. 启动时建立运行环境

| 顺序 | 发生的事 | 代码模块 |
| --- | --- | --- |
| 0.1 | 读取 `settings.json`，取得模型、路径、模型目录和超时等配置；API 密钥只从配置指定的环境变量读取。 | `config.py`、`tools_registry.py:build_client()` |
| 0.2 | 创建长期单例：向量记忆库、OCR、Listener 和计划管理器。 | `initer.py`、`Saver.py`、`Visal.py`、`listener.py`、`plan.py` |
| 0.3 | 注册公共工具及其 JSON schema。函数签名和 docstring 被转换为模型可用的工具描述。 | `Tools.py`、`tools_registry.py:register_common_tools()` |
| 0.4 | 创建 Bus，标记慢任务、危险任务；创建 CapabilityGateway 并登记当时已注册工具。 | `super.py`、`event_bus.py`、`runtime/gateway.py` |
| 0.5 | 注册专家工具与可选的 `run_workflow`；建立 Super 的初始消息历史。 | `tools_registry.py:build_super_tools()`、`experts.py`、`super.py` |

注意：`super.py` 在注册 Gateway 后才注册专家工具和 Resident 管理工具；这些后注册工具目前没有被登记进 Gateway。因此它们可被传统 Super 调用，但不能可靠地出现在 Gateway 保护的显式工作流中。这是当前接线顺序的限制。

## 1. 接收用户任务

1. `super.py:main()` 通过 `Bus.io_dialog()` 显示输入框，并使用 IO 锁避免后台任务与用户输入交错。
2. 输入文本以 `{role: "user", content: ...}` 追加到 `messages`。
3. Super 的 `Agent.run_agent()` 被调用。Super 看到公共工具与专家工具，但连续监听的有状态工具被 `LISTENER_STATEFUL_TOOLS` 拒绝，避免多个角色竞争同一份 Listener 状态。
4. `Agent.run_agent()` 根据工具 schema 请求 OpenAI 兼容接口。模型输出纯文本则任务本轮结束；输出 `tool_calls` 则进入下一步。

对应模块：`super.py` → `Agent.py` → `Tools.py`。

## 2. Agent 调用工具的通用循环

这一段适用于一次任务中所有普通工具、专家工具以及子 Agent 内部工具调用。

1. `Agent.run_agent()` 解析每个 tool call 的 JSON 参数；解析失败时以 `role: tool` 错误消息反馈给模型修正。
2. Agent 先做执行层权限检查：专家只能调用 `EXPERTS` 中的白名单工具；Super 的 deny list 会拦截 Listener 状态工具。
3. 非 Gateway 路径直接调用 `Bus.submit()`；Gateway 路径先调用 `CapabilityGateway.execute()`，再由它转交 `Bus.submit()`。Gateway 是显式工作流中的正式权限边界。
4. Bus 根据工具类型执行：
   - 快任务由 `_ans_executer()` 立即执行，并把 `Done` 或 `Error` 结果返回给 Agent；Agent 将它作为对应 tool call 的 `role: tool` 消息追加到历史。
   - 慢任务立即返回 `Submitted(task_id)`，由 Bus 后台执行。Agent 保存 `tool_call_id → task_id` 映射，并先回填“已提交”的 tool 消息，保证 API 的消息配对合法。
5. 对慢任务，Agent 的 `_wait_pending()` 以固定间隔调用 `Bus.poll()`，但不消耗模型循环步数。完成结果放入 `delayed_results`；模型需要时调用 `view_delayed_results` 读取。它不会再次复用原来的 `tool_call_id` 回填结果。
6. 模型收到工具结果后决定下一次调用或直接作答；最多执行 `settings.json.runtime.agent_max_steps` 次模型回合。

危险工具（目前包括记忆删除/替换，terminal 中还包括新增记忆）会在 Bus 执行前通过 `io_dialog()` 询问用户 `y/n`。并发任务由 `event_bus.py` 的 semaphore 限制；可重入专家任务不占该锁，避免“专家持锁后又等待内部工具”造成死锁。

对应模块：`Agent.py:Agent.run_agent()`、`Agent.py:_wait_pending()`、`event_bus.py:Bus.submit()/poll()`、`runtime/gateway.py:CapabilityGateway.execute()`。

## 3. 普通“专家路由”流程

当 Super 模型认为任务需要专家，会调用例如 `math_expert(task=...)` 或 `mathwrite_expert(task=...)`。

1. 这些函数由 `build_super_tools()` 动态生成，并标为 Bus 慢任务和可重入任务。
2. 专家函数从 Super 的 `messages` 抽取用户与 Super 的纯文本对话，调用 `build_task_context()` 创建内存中的 `Task` 与 `TaskContext`。
3. 专家函数新建一个 `Agent`，并以专家 prompt、任务摘要为新消息历史执行它。专家不会继承 Super Agent 的 pending、延迟结果或完整 tool-call 历史。
4. 专家 Agent 仅看到并能执行该专家白名单中的工具。例如 mathwrite 可写 LaTeX、查定理规范、检查 LaTeX；math 专家没有写文件权限。
5. 专家返回文本作为慢任务结果，被 Super 放入 `delayed_results`。Super 可读取该结果、继续路由下一位专家或向用户回答。

这一流程中 `Task` / `TaskContext` 只是在内存中组织本次调用；普通专家路由不会保存任务状态、阶段结果或产物版本，也不会自动把多位专家串成强约束流程。

对应模块：`tools_registry.py:build_super_tools()`、`tools_registry.py:build_task_context()`、`experts.py`、`runtime/task.py`、`runtime/context.py`。

## 4. 显式 `run_workflow` 流程

当 Super 模型调用 `run_workflow(goal, stage_tasks)` 时，当前实现提供一个线性的阶段执行器。

1. LLM 自己生成 `stage_tasks`，每项为 `{"expert": "...", "task": "..."}`。宿主只校验专家名是否存在；没有固定模板、依赖图或产物声明。
2. `tools_registry.py` 创建 `Task`，并将每项转换为 `runtime.workflow.Stage`。Stage 的工具集合来自该专家的工具白名单。
3. `Workflow.run()` 把 Task 标记为 `RUNNING`，按列表顺序取出下一个 Stage。
4. 对每一个 Stage，`Workflow.derive_passport()` 从 Super 的父 Passport 派生更窄的 Passport；`CapabilityGateway.derive()` 会丢弃父 Passport 没有的能力，不能扩权。
5. runner 创建一个新的子 Agent，以上一个阶段的**文本结果**作为 `TaskContext.previous_results`，使用该阶段 Passport 运行。子 Agent 的 schema 和真正可执行工具均由 Gateway 裁剪。
6. runner 返回文本，`Workflow.run()` 将其作为下一阶段唯一的 `prev` 值；最后阶段成功后把 Task 标记为 `DONE`，聚合各阶段文本后返回 Super。
7. 任一 runner 抛出 `StageAbort` 或普通异常时，Task 标记为 `FAILED`，后续阶段不再执行。

当前的“分步执行”到此为止：顺序、权限收窄和中止语义已经存在；但任务拆分仍由 LLM 临时决定，输入/输出是自由文本，且 Task、Passport、阶段结果均不持久化。也没有重试、回滚、条件分支、并行阶段和宿主侧产物验收。

对应模块：`tools_registry.py:run_workflow()`、`runtime/workflow.py`、`runtime/gateway.py`、`runtime/passport.py`、`runtime/task.py`。

## 5. 已完成度更高的特例：课堂笔记工作流

notetaker 是当前唯一具有明确阶段语义和独立验收的工作流。

1. Listener 后台线程取得转写后，向 Bus 发布 `listener.transcript_updated`。`ResidentAgent` 同时也可被定时器或手动 `poke` 唤醒。
2. `ResidentAgent._tick_workflow()` 的 `observe` 阶段从 Listener 读取 `last_cursor` 之后的增量文本，但不提交 cursor。
3. `write_notes` 阶段使用只包含写文件、LaTeX 检查等权限的 Passport，运行 notetaker Agent 更新 `latex_output/notes.tex`。
4. `verify_latex` 阶段由宿主直接经 Gateway 再次执行 `check_latex`，不信任模型“我已检查”的声明。
5. 只有校验结果包含成功标记，`commit_cursor` 才推进 `last_cursor`；否则抛出 `StageAbort`，下次唤醒重放同一段转写。

这个流程证明现有 `Workflow` 可以承载宿主引导的阶段、独立验收与失败重放，但 cursor 和运行记录仍只在进程内；重启后无法恢复未完成轮次。

对应模块：`listener.py`、`resident.py:build_lecture_note_workflow()`、`resident.py:ResidentAgent._tick_workflow()`、`runtime/workflow.py`。

## 6. 当前任务完成的含义

对普通对话，“完成”是 Super Agent 产生不再含工具调用的文本；对 `run_workflow`，“完成”是全部 Stage runner 正常返回且 Task 被标记 `DONE`；对课堂笔记，还必须通过独立 LaTeX 校验并提交 cursor。三者目前都只保存在内存和文件副作用中，没有统一的可恢复任务记录。

若要让任务在重启后继续，下一步应为 `Task`、`Workflow`、Stage 结果、消息历史和文件产物引用增加同一套持久化存储，并将每个阶段的“开始、产物写入、验收、提交”做成原子记录。
