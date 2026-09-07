好，这一部分我直接按照你**当前真实代码**来做，而不是继续停留在抽象架构层。下面的判断以你目前的 `Agent.py / event_bus.py / super.py / resident.py / plan.py` 为基础；例如当前 `Agent.run_agent()` 确实同时承担 schema 裁剪和执行层白名单，`super.py` 中专家也是通过 `_names` 注入工具子集，`resident.py` 直接持有 `tool_names`，而 `plan.py` 本质上只是文件型日程/待办存储。   

# 第五部分：逐文件映射到 v2 Runtime

先给结论：

| 文件             | v2 定位                                | 处理                      |
| -------------- | ------------------------------------ | ----------------------- |
| `Agent.py`     | Agent Runtime / Execution Client     | **保留，瘦身**               |
| `event_bus.py` | Data Plane / Execution Bus           | **保留，重新划边界**            |
| `super.py`     | Control Plane / Bootstrap + Super    | **大幅抽离职责**              |
| `resident.py`  | Autonomous Runtime / Workflow Worker | **保留，接入 Task/Passport** |
| `plan.py`      | Persistent Plan Ledger               | **保留，不升级成 Planner**     |

而新增核心：

```text
runtime/
├── task.py
├── context.py
├── workflow.py
├── passport.py
├── capability.py
├── gateway.py
└── protocol.py
```

---

# 一、`Agent.py`：保留，但必须去掉“鉴权中心”身份

目前 `Agent.py` 是整个系统最关键的 Runtime。

它已经有几个非常成熟的东西：

```text
LLM loop
    ↓
tool schema
    ↓
tool call
    ↓
fast / slow task
    ↓
pending
    ↓
poll
    ↓
delayed_results
```

例如目前它自己维护：

```python
self.pending
self.delayed_results
```

并且 poll Bus 的慢任务，将完成结果缓存起来。

**这些都应该保留。**

真正应该删除的是：

```python
tool_names
deny_tools
```

作为权限机制的地位。

---

## v2 的 Agent

未来：

```python
class Agent:

    async def run(
        self,
        context: TaskContext,
        passport: Passport,
    ):
        ...
```

Agent 做四件事情：

```text
1. 构造 prompt
2. 获取可见 ToolSpec
3. 请求 LLM
4. 把 tool_call 交给 Gateway
```

而不负责：

```text
× 判断有没有权限
× 决定当前阶段能用什么
× 修改 Passport
× 决定某个 Tool 是否授权
```

---

## 当前代码怎么迁移

现在：

```python
allow = set(tool_names)
deny = set(deny_tools)

schema = ...
```

这段逻辑应该最终变成：

```python
schema = gateway.visible_tool_schema(passport)
```

然后：

```python
result = await gateway.execute(
    passport=passport,
    tool_call=tool_call,
)
```

---

## 但不要立即删除旧 API

第一阶段应该允许：

```python
run_agent(
    ...,
    tool_names=["view", "check_latex"]
)
```

继续工作。

内部做 adapter：

```text
tool_names
   ↓
LegacyPolicy
   ↓
Passport
   ↓
Gateway
```

这样整个系统可以渐进迁移。

### 因此：

**保留：**

* `pending`
* `delayed_results`
* LLM loop
* fast/slow task 处理
* `max_step`
* tool-call JSON 解析
* delayed result polling
* message history

**抽离：**

* `tool_names` 权限判断
* `deny_tools` 权限判断
* Tool schema policy

**最终废弃：**

```python
allow = set(tool_names)
deny = set(deny_tools)
```

作为正式权限系统。

---

# 二、`event_bus.py`：不要废掉，它应该成为 Data Plane

这一点我反而比之前更坚定：

> **不要重写 EventBus。**

它现在已经承担了真正的 Runtime 基础设施。

包括：

```text
并发控制
slow task
reentrant task
task result
event callback
thread-safe event
IO lock
```

当前 Bus 已经有：

```python
task_results
slow_tasks
reentrant_tasks
dangerous_tools
event_callbacks
semaphore
```

这些都是有价值的 Runtime 状态。

---

## 但 Bus 有一个根本问题

现在它同时理解：

```text
Tool
Task
Event
Dangerous
Slow
Agent
IO
```

所以会逐渐变成：

> 万能系统对象。

这必须控制。

---

# v2 Bus 的职责

我建议最终定义：

```text
EventBus
    │
    ├── Command delivery
    ├── Event delivery
    ├── Task execution
    ├── Result storage
    └── Runtime concurrency
```

但是：

```text
× Authorization
× Workflow
× Passport issuing
× Task planning
× Agent identity
```

全部交给 Control Plane。

---

# 三、一个非常重要的改变：Gateway → Bus

未来调用链：

```text
Agent
  ↓
Gateway
  ↓
authorize
  ↓
Bus.submit()
  ↓
Tool
```

而不是：

```text
Agent
  ↓
Bus.submit()
  ↓
Tool
```

换句话说：

> **Bus 是执行基础设施，Gateway 是执行入口。**

如果以后发现代码里还有大量：

```python
bus.submit(...)
```

散落在 Agent/Resident/Super 中，要逐渐收敛。

最终只有：

```text
Gateway
Workflow internal executor
System bootstrap
```

可以直接把 Command 送进 Bus。

---

# 四、EventBus 的 `emit/publish` 不要现在重写

当前 Bus 已经有两套事件机制：

```python
emit()
publish()
```

其中 `publish()` 是直接执行 callback，而 `emit()` 是把 Event 放入消息队列。

这是历史演化留下的双轨。

**现在不要重写。**

先定义语义：

```text
publish
    = immediate local notification

emit
    = asynchronous event delivery
```

然后未来再统一。

否则这次重构会一下子变成“重写整个消息系统”。

---

# 五、`super.py`：这是重构最大的地方

目前 `super.py` 做了太多事情。

它现在至少负责：

```text
配置
专家定义
工具注册
公共工具注册
OpenAI client
Renderer
Bus
Super Agent
ResidentManager
Listener bridge
启动循环
```

例如 `main()` 同时创建 Tools、client、renderer、Bus、Super Agent、专家工具、ResidentManager 和 Listener bridge。

这就是未来最应该拆的文件。

---

# 六、Super 真正应该留下什么？

最终：

```text
super.py
```

应该只负责：

```text
Super Controller
```

也就是：

```text
User request
     ↓
Task
     ↓
Context
     ↓
Workflow
     ↓
Dispatch
     ↓
Observe
     ↓
Verify
     ↓
Replan
```

---

# 七、现在的 `EXPERTS` 怎么处理？

目前：

```python
EXPERTS = {
    "math": (..., tool_names),
    "mathwrite": (..., tool_names),
    ...
}
```

这是当前架构最大的“硬编码权限”。

应该拆成：

```text
AgentDefinition
```

例如：

```python
AgentDefinition(
    id="math",
    prompt=...,
    capabilities={
        "filesystem.read",
        "memory.read",
    },
)
```

注意：

**不要直接把工具名搬过去。**

否则只是：

```text
EXPERTS["math"].tool_names
```

变成：

```text
EXPERTS["math"].capabilities
```

还是静态权限。

---

# 八、专家应该变成 Principal

例如：

```text
math_expert
    Principal
        ↓
Math Agent
```

然后某个 Task：

```text
Task #123
Stage = mathematical_analysis
```

Gateway：

```text
issue passport
```

得到：

```text
Math Agent
    ├── latex.read
    ├── memory.read
    └── filesystem.read
```

而另一个 Task：

```text
Stage = latex_generation
```

即使还是 Math Agent：

```text
Math Agent
    ├── latex.read
    ├── latex.write
    └── latex.compile
```

这就是：

> **Agent identity 与 Task capability 分离。**

这是整个设计最重要的收益之一。

---

# 九、`build_super_tools()` 应该逐步消失

目前它把：

```text
expert
```

注册成 Bus Tool：

```text
Super
 ↓
math_expert(...)
 ↓
new Agent(...)
```

这是现在合理的实现方式，但它是一个过渡结构。

目前代码中确实是每次调用专家创建独立 Agent，再把 `_names` 作为工具白名单传进去。

未来：

```text
Super
 ↓
WorkflowNode
 ↓
AgentRuntime
 ↓
Passport
 ↓
MathAgent
```

也就是说：

> “调用专家”最终不是一次 Tool Call，而应该是一种 **Agent Dispatch Command**。

---

# 十、不过第一阶段不要废掉专家 Tool

这点非常重要。

v1：

```text
Super → math_expert tool → Agent
```

v2：

```text
Super → DispatchAgent(math) → Agent
```

但是第一阶段可以：

```text
DispatchAgent
    ↓
adapter
    ↓
old math_expert
```

所以旧系统继续运行。

---

# 十一、`_dialogue_context()` 应该降级为 Compatibility Layer

目前专家任务会把 Super 与用户的纯文本历史抽出来，再塞入专家 prompt。

这现在是合理 workaround。

但未来：

```text
Super messages
```

不应该继续作为专家之间的主要状态同步机制。

应该：

```text
Super
 ↓
TaskContext
 ↓
RelevantContext
 ↓
Expert
```

例如：

```python
context = TaskContext(
    goal="将公式整理成 LaTeX",
    artifacts=["equation.tex"],
    constraints=[...],
    previous_results=[...],
)
```

Expert 只得到相关内容。

---

# 十二、`resident.py`：不要把它当普通 Agent

这个文件其实很有价值。

当前 Resident 本身已经具备：

```text
周期唤醒
事件唤醒
持续 messages
Listener cursor
独立 Agent
```

例如它会监听：

```text
listener.transcript_updated
```

并通过 `wake_event` 立即唤醒。

所以我认为：

> **Resident 是你未来 Autonomous Agent 的原型，不应该削弱。**

---

# 十三、Resident v2

当前：

```python
ResidentAgent(
    tool_names=...,
    deny_tools=...
)
```

未来：

```python
ResidentAgent(
    principal=...,
    workflow=...,
)
```

运行时：

```text
Trigger
 ↓
Create Task
 ↓
Create TaskContext
 ↓
Determine Stage
 ↓
Gateway.issue()
 ↓
Agent.run()
 ↓
Verify
 ↓
Commit
```

---

# 十四、Notetaker 特别适合变成 Workflow

当前 `_build_trigger()` 已经实际上写死了一个工作流：

```text
Listener 新增转写
 ↓
整理课堂笔记
 ↓
更新 notes.tex
 ↓
check_latex
```

代码中确实明确要求更新 `latex_output/notes.tex` 后调用 `check_latex`。

这已经不是普通 Prompt 了。

它实际上就是：

```text
Workflow: LectureNoteTaking
```

建议未来显式化：

```text
LectureNoteTaking
├── ObserveTranscript
├── Understand
├── WriteNotes
├── VerifyLatex
└── CommitCursor
```

这会成为你整个 Workflow 系统最好的第一个真实案例。

---

# 十五、而且这里存在一个必须修的事务问题

当前：

```python
cursor = get_listen_cursor()

new_text = get_listen_result(
    since_index=self.last_cursor
)

self.last_cursor = cursor
```

**在 LLM 写入和 LaTeX 检查之前，cursor 已经推进。** 

因此未来必须变成：

```text
C = current_cursor

read(C)
 ↓
process
 ↓
write notes
 ↓
check_latex
 ↓
SUCCESS
 ↓
commit cursor = C
```

失败：

```text
cursor 保持 C
```

这不是“优化”。

这是 Resident Workflow 的一致性保证。

---

# 十六、`Plan.py`：千万不要升级成 Planner

这个文件我反而建议：

> **基本原样保留。**

因为它现在做的事情很清楚：

```text
plan/YYYYMMDD.md
plan/general.md
```

分别保存每日和长期待办。

它提供：

```text
add_today_plan
view_plan
add_general_plan
view_general_plan
```

这就是一个：

> **Persistent Plan Ledger**

非常合理。

---

# 十七、Plan 和 Workflow 必须分开

这是未来非常容易混淆的一点。

### Plan

回答：

> 我要做什么？

例如：

```text
- 完成论文第三章
- 整理今天课堂笔记
- 推导 Klein-Gordon 方程
```

### Workflow

回答：

> 这个东西具体怎么做？

例如：

```text
整理课堂笔记：

Observe
→ Understand
→ Edit
→ Compile
→ Verify
→ Commit
```

因此：

```text
Plan
  ↓
Task
  ↓
Workflow
```

而不是：

```text
Plan = Workflow
```

---

# 十八、所以 Plan.py 不应该加入 Task 状态机

不要给它塞：

```text
RUNNING
FAILED
WAITING
VERIFYING
```

这些属于：

```text
TaskContext
Workflow
```

Plan 只负责持久化用户层计划。

---

# 十九、最终五个文件的责任边界

我建议最终明确成这样：

```text
Agent.py
────────────────────────
LLM Agent Runtime
Tool-call loop
Pending
Delayed result
Conversation
```

```text
event_bus.py
────────────────────────
Command/Event transport
Async execution
Concurrency
Slow task
Result storage
```

```text
super.py
────────────────────────
Super Controller
Task orchestration
Workflow selection
Agent dispatch
Replan
```

```text
resident.py
────────────────────────
Autonomous trigger loop
Resident lifecycle
Observation → Task
```

```text
plan.py
────────────────────────
Persistent human plan ledger
```

而新增：

```text
runtime/capability.py
────────────────────────
Capability semantics

runtime/passport.py
────────────────────────
Capability grant

runtime/gateway.py
────────────────────────
Authorization + execution boundary

runtime/task.py
────────────────────────
Task lifecycle

runtime/context.py
────────────────────────
Structured working state

runtime/workflow.py
────────────────────────
Executable workflow

runtime/protocol.py
────────────────────────
Command/Event/Result/Signal envelope
```

---

# 二十、最重要的依赖关系

最终应该变成：

```text
                     ┌──────────────┐
                     │    Super     │
                     └──────┬───────┘
                            │
                            ▼
                     ┌──────────────┐
                     │     Task     │
                     └──────┬───────┘
                            ▼
                     ┌──────────────┐
                     │ TaskContext  │
                     └──────┬───────┘
                            ▼
                     ┌──────────────┐
                     │  Workflow    │
                     └──────┬───────┘
                            ▼
                        Stage
                            │
                            ▼
                  ┌──────────────────┐
                  │ Capability       │
                  │ Gateway          │
                  └────────┬─────────┘
                           ▼
                       Passport
                           │
                  ┌────────┴─────────┐
                  ▼                  ▼
               Agent             Resident
                  │                  │
                  └────────┬─────────┘
                           ▼
                         Tool
                           │
                           ▼
                      EventBus
                           │
                           ▼
                       Artifact
                           │
                           ▼
                        Verify
                           │
                           ▼
                     TaskContext
```

这已经是一个完整的 Runtime 架构了。

---

# 二十一、给 CC 的实际重构路线

我建议你**严格分 5 个 PR / 5 个阶段**。

不要让 CC 一次性改。

---

## Phase 1：Capability Gateway

目标：

> **零行为变化，只建立新权限层。**

新增：

```text
runtime/
    capability.py
    passport.py
    gateway.py
```

实现：

```python
Capability
CapabilityGrant
ToolPolicy
Passport
CapabilityGateway
```

要求：

```text
旧 tool_names
      ↓
LegacyAdapter
      ↓
Passport
      ↓
Gateway
```

必须保证现有：

```text
Super
Math
MathWrite
Resident
Listener
```

全部继续运行。

### 验收

必须测试：

```text
允许工具 → 成功
不允许工具 → Gateway 拒绝
schema 裁剪 → 正常
deny → 正常
Passport 修改 → 不允许
```

---

# 二十二、Phase 2：Agent 接入 Passport

修改：

```text
Agent.py
```

让：

```python
run_agent(...)
```

支持：

```python
passport=Passport
```

工具 schema 改为：

```python
gateway.visible_tools(passport)
```

工具执行改为：

```python
gateway.execute(passport, tool_call)
```

但是保留：

```python
tool_names=
deny_tools=
```

作为 deprecated compatibility API。

---

# 二十三、Phase 3：TaskContext

新增：

```text
runtime/task.py
runtime/context.py
```

建立：

```python
Task
TaskStatus
TaskContext
ArtifactRef
VerificationState
```

先不要做复杂持久化。

第一阶段：

```text
Super 创建 Task
 ↓
TaskContext
 ↓
专家获得结构化 context
```

然后逐渐减少：

```python
_dialogue_context()
```

依赖。

---

# 二十四、Phase 4：Workflow / Stage

新增：

```text
runtime/workflow.py
```

先只实现：

```python
Workflow
Stage
WorkflowResult
```

**不要立即做 DAG 编辑器。**

甚至第一版：

```python
Workflow(
    stages=[
        Stage("understand"),
        Stage("write"),
        Stage("verify"),
    ]
)
```

就够了。

然后：

```text
Stage
 ↓
Gateway.derive()
 ↓
new Passport
```

实现工具动态裁剪。

---

# 二十五、Phase 5：Resident Workflow

最后再改：

```text
resident.py
```

把：

```text
tool_names
deny_tools
```

彻底替换成：

```text
Workflow
Principal
TaskContext
Passport
```

然后首先把 Notetaker 做成：

```text
LectureNoteTakingWorkflow
```

它会成为你整个系统第一个真正完整的：

```text
Observe
→ Plan
→ Act
→ Verify
→ Commit
```

闭环。

---

# 二十六、CC 第一阶段可以直接使用的规格

下面这段建议你**原样交给 CC**，不要让 CC 自己发挥架构。

# Phase 1 Refactor Specification: CapabilityGateway + Passport

## Objective

在现有多 Agent 系统中增加一个系统级 CapabilityGateway 与 Passport 层。

本阶段的唯一目标是：

> 将现有 `Agent.run_agent(tool_names=..., deny_tools=...)` 的工具授权逻辑迁移到 CapabilityGateway，但保持现有运行行为完全不变。

不要在本阶段引入 Workflow、TaskContext、Task Graph、DAG、Memory 重构或 EventBus 大改。

---

## 1. 新增模块

创建：

```text
runtime/
    __init__.py
    capability.py
    passport.py
    gateway.py
```

### capability.py

定义：

```python
Capability
CapabilityGrant
ToolPolicy
```

Capability 表示系统能力语义，不要直接等同于 tool name。

至少支持：

```text
capability name
permission
visibility
priority
constraints
```

`ToolPolicy` 至少支持：

```text
permission:
    allow / deny

visibility:
    hidden / normal / emphasized

priority:
    int
```

---

## 2. Passport

定义不可变：

```python
@dataclass(frozen=True)
class Passport:
    passport_id: str
    principal_id: str
    task_id: str | None
    stage_id: str | None
    grants: tuple[CapabilityGrant, ...]
    expires_at: datetime | None
    parent_passport_id: str | None
```

要求：

1. Agent 不能修改 Passport。
2. Agent 不能自行扩大 grants。
3. Passport 由 CapabilityGateway 创建。
4. Passport 必须包含 principal identity。
5. 不要实现 JWT/OAuth/密码学签名；当前系统是单进程个人 Agent 系统，使用 Gateway 内部 registry 即可。

---

## 3. CapabilityGateway

定义：

```python
class CapabilityGateway:

    def issue(...)->Passport:
        ...

    def derive(...)->Passport:
        ...

    def visible_tools(
        self,
        passport: Passport,
    ) -> list:
        ...

    def authorize(
        self,
        passport: Passport,
        tool_call,
    ):
        ...

    async def execute(
        self,
        passport: Passport,
        tool_call,
    ):
        ...
```

Gateway 是正式的 execution authorization boundary。

任何 Agent tool call 在进入 EventBus/Tool handler 之前，都必须经过 Gateway.authorize/execute。

---

## 4. ToolSpec

不要破坏当前 Tools API。

可以先增加一个 adapter，把现有：

```text
tool name
schema
handler
timeout
slow
dangerous
reentrant
```

转换成内部 ToolSpec。

ToolSpec 至少需要：

```text
name
schema
handler
capabilities
timeout
side_effect
resources
idempotent
```

但本阶段只要求 capabilities / policy 真正参与授权。

其他字段可以先作为 metadata。

---

## 5. Legacy compatibility

当前代码使用：

```python
run_agent(
    tool_names=...,
    deny_tools=...
)
```

不能立即删除。

实现：

```text
legacy tool_names
        ↓
LegacyPolicyAdapter
        ↓
Passport
        ↓
CapabilityGateway
```

因此旧代码仍然可以运行。

不要在 Phase 1 修改所有调用方。

---

## 6. Agent.py

只做最小修改：

原：

```text
LLM
 ↓
tool_names schema filtering
 ↓
bus.submit
```

改为：

```text
LLM
 ↓
Gateway.visible_tools(passport)
 ↓
LLM tool call
 ↓
Gateway.execute(passport, tool_call)
 ↓
Bus
```

保留：

```text
pending
delayed_results
slow task polling
max_step
LLM message loop
```

不要重写 Agent loop。

---

## 7. Authorization semantics

必须严格区分：

```text
authorization
visibility
emphasis
```

例如：

```text
DENY
```

表示不可执行。

```text
ALLOW + hidden
```

表示可以执行，但不向 LLM 暴露。

```text
ALLOW + normal
```

正常暴露。

```text
ALLOW + emphasized
```

暴露并提高工具优先级。

重要：

> Schema filtering 不是安全边界。

真正安全边界必须是：

```text
Gateway.execute()
```

---

## 8. EventBus

本阶段不要重写 EventBus。

只要求：

```text
Agent → Gateway → Bus
```

成为新的正式执行路径。

保留现有：

```text
slow task
reentrant task
dangerous tool
task result
event callbacks
IO lock
```

如果现有内部代码必须直接调用 `bus.submit()` 才能保持兼容，可以暂时保留，并明确标记为 legacy/internal path。

---

## 9. Super / Resident

Phase 1 不要求彻底迁移。

允许：

```python
Super → run_agent(tool_names=...)
Resident → run_agent(tool_names=...)
```

继续工作。

它们通过 compatibility adapter 自动获得 Passport。

不要在 Phase 1 修改 Super workflow。

不要在 Phase 1 修改 Resident cursor 逻辑。

---

## 10. Tests

必须增加测试：

### Authorization

```text
allowed tool → execute
denied tool → reject
```

### Visibility

```text
hidden → not included in schema
normal → included
emphasized → included with higher priority
```

### Passport immutability

```text
Agent cannot mutate grants
Agent cannot add capability
```

### Legacy

```text
tool_names=["view"]
```

必须与当前行为一致。

### Regression

至少确保：

```text
Super
Math
MathWrite
Resident
slow task
delayed_results
```

不发生行为回归。

---

## 11. Explicit non-goals

本阶段禁止：

```text
× 重写 EventBus
× 重写 Agent loop
× 删除 tool_names
× 引入 Workflow
× 引入 TaskContext
× 修改 Plan.py
× 修改 Memory
× 修改 Listener
× 引入 DAG
× 引入 JWT/OAuth
× 引入远程 RPC
× 大规模目录重构
```

完成 Phase 1 后停止，输出：

1. 修改了哪些文件
2. 新增了哪些类
3. 当前执行链路
4. 测试结果
5. 哪些旧 API 仍然兼容
6. 下一阶段建议

不要自动进入 Phase 2。

---

# 二十七、还有一个我建议你现在就定下来的原则

这次重构中，**不要让 CC 因为看到“架构设计”就开始过度工程化。**

尤其警惕它自动生成：

```text
AbstractAgentFactory
BaseCapabilityProvider
CapabilityResolverFactory
PassportManager
AuthorizationService
PermissionStrategy
PolicyProvider
...
```

最后出现 20 个类，只为了执行：

```python
if "view" in allowed_tools:
```

你的系统是个人 Agent OS，不是 Kubernetes。

第一版真正需要的其实就是：

```text
Capability
Passport
Gateway
```

三个核心原语。

然后：

```text
TaskContext
Workflow
Stage
```

再成为上层控制结构。

---

# 二十八、第五部分最终结论

如果把这次重构压缩成一句话：

> **不要把现有系统推倒重来；把 `Agent.py` 从“既是 Agent 又负责鉴权”解放出来，把 `super.py` 从“万能启动脚本”逐步升级成 Control Plane，把 `event_bus.py` 稳定成 Data Plane，把 `resident.py` 发展成 Autonomous Workflow Runtime，而让 `plan.py` 安静地继续做它现在做得很好的 Plan Ledger。**

最终你真正拥有的是：

```text
                  CONTROL PLANE
────────────────────────────────────

User
 ↓
Super
 ↓
Task
 ↓
TaskContext
 ↓
Workflow
 ↓
Stage
 ↓
CapabilityGateway
 ↓
Passport


                  DATA PLANE
────────────────────────────────────

Passport
 ↓
Agent
 ↓
Tool
 ↓
EventBus
 ↓
Execution
 ↓
Artifact
 ↓
Observation
 ↓
Verification
```

而这两层之间的**唯一正式桥梁就是 Gateway**。

我认为这比单纯做一次“鉴权代码重构”重要得多：它实际上是在给你现在已经相当成熟的 Agent/Tool/Event Runtime **补上控制平面**。目前代码已经有 slow-task、reentrant、dangerous-tool、event callback 等运行时机制，因此继续堆这些基础设施的边际收益已经明显下降；下一阶段最值得投资的就是把“任务状态—阶段—能力—执行—验证”串起来。
