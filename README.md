# single_agent_trail
an trail for single agent

问题（已解决）：
tool_call_id是独一无二的吗？他的生成机制是什么？
    答：tool_call_id 是「OpenAI 兼容 API 服务端」为每个工具调用生成的字符串标识（形如 call_xxx），
    由服务端生成、客户端不可指定。它保证「同一段对话(messages)历史内唯一」，用于把 role:tool
    消息关联回对应的 tool_call。因此可作为 pending 表的 key（tool_call_id -> task_id），
    但只能用于「同一对话会话内」跟踪，不能跨对话/跨请求假定唯一。
    注意：LLM 每发起一次 tool_call，都必须回填一个带相同 tool_call_id 的 role:tool 消息，
    否则服务端会报错（tool_call_id 缺少对应 tool 消息）。

这是一个个人使用的LaTeX编写与数学物理讨论一体化Agent，同时工程代码与理论代码有所不同，它不侧重于抽象问题在不断扩充，而是着眼于解决问题
高消耗/CPU 密集/阻塞函数 → 注册为同步函数（走 to_thread）；只有 IO 等待型函数 → 才写成协程。

通过bus的返回格式：None或者
返回格式: 一个 Message 对象
{
    'title' : str = 'Done' , 'Submitted' , 'Error'
    'content' : dict : 
    {
        若没有故障，为被调用函数的返回结果，视具体函数而定
        'id' : int 调动序列的编号
        'error' : str 错误内容
        'error_type' : str 错误类型
        'Task' : str 任务名
        'args' : dict 参数列
    }
}

所有功能函数以dict方式返回信息

如果一个进程希望调度一个自己不能调度的函数，需要向Super发送一个提醒，该怎么办？
    走emit? 然后让专家之间相互订阅，在有必要的时候通过emit唤醒所有专家

目标：
准备实现的内容：
加入MCP合作机制，设计结构：
    一个Super用来接收用户请求，进行请求理解，流程设计(设计为一张又向图)，并且在用户许可时可以更改其他Agent的提示词，同时他要负责在每次对话将要结束时对对话进行复盘，判断哪些功能在之后的工作中有被集成为Tool的价值

    一个Math专家负责对OCR给出的结果进行逻辑判断，它不能直接写入代码，只能读取文件，并与用户讨论数学问题；他需要在讲解时保持亲和力，并不断对用户追问确保用户理解正确，它还可以通过设置谬论的方式对用户进行困扰，引导用户思考问题本质。

    一个MathWrite专家负责将其他Agent的输出转写为LaTeX代码，在涉及到定理，引理，定义，证明的书写时，他必须根据tools的指示进行填写，同时确保自己所使用的LaTeX代码正确，它还要利用这项能力辅助Math对OCR检验。

    一个PassageWrite的专家负责设计篇章结构，管理MathWrite的编写位置，并将Super,Math与用户的对话总结（不是摘录）成重点突出的LaTeX文档

    一个Draw负责进行Tikz绘图

    一个Listener负责监听音频API接口，有必要时汇报给PassageWriter与Super对课堂内容进行记录，

功能：
    联网功能
    LaTeX编写相关功能
    听写功能
    流式IO支持

已实现功能：

    单个Agent，支持异步
    本地OCR
    使用Tools对工具进行注册
    一个未全面竣工的本地保存数据库

OCR 改动：
    - recognize_doc / recognize_image 由 async 改为同步阻塞函数（不再内部 to_thread），
      统一交由 Tools.async_execute 的 to_thread 分支调度到线程池，结构更简单。
    - 新增缓存：OCR 结果存为 JSON 于 OCR_result/ 目录，长期存储。
      PDF 按「页」缓存（文件名 + 内容 md5 + 页码），单张图片按「文件」缓存；
      每次 OCR 前检查缓存，命中直接跳过 OCR（内容变更自动失效重识别）。


程序结构：
    Agent cyc waiting for message
    Agent_1->Bus->Agent_2

    Agent_exe->tool

消息总线：
    将所有模块间的消息通过总线传递
    消息总线上挂载的executer负责将msg解包为args并通过对应的tool.executer执行

    这里有一个问题：executer执行之后，对方不知道自己因该在哪里播报自己的结果，但是这不是问题，我们把一个约定的字段设为recall来表示回拨路径

待办（TODO，高级特性，已注释暂缓开放）：
    - immediate_start_ans：让 Agent 选择「立即开始回答，不等待后续工具结果」（Agent.py 已注释）
    - pending_release：让 Agent 放弃所有在途慢任务（Agent.py 已注释）
    - _released_tasks：配合 pending_release 的「已放弃任务集合」（event_bus.py 已注释）
    - 兜底策略：慢任务在 max_step 内未完成时，如何避免结果丢失（方案：慢任务硬超时 + 「等待不烧 step」）
    - recall 回拨字段的实际接线：让「异步回调」模式落地（目前 Message.reply 字段已预留，未接线）
    - 不可中断重任务的优雅停止：to_thread 无法 cancel 阻塞线程，考虑独立线程池 / 进程池（ProcessPoolExecutor）

多智能体架构（进行中）：
    - 专家不常驻：各专家均接入智能体，由智能体自己判断「是否完成工作、可以停止」；
      过渡期先由人给指令停止，不单独做「常驻线程 + 挂起」模型。
    - Super 也是专家，但只负责路由，不负责具体读写。
    - 所有 Agent 共用同一份 Tools（挂在 bus 上）；「不同 Agent 不同工具」由之后单独的鉴权系统实现。
    - 专家间通过总线 emit/subscribe 相互唤醒（松耦合）。
    - 控制台输出互斥：多智能体共享控制台，A 输出时 B 必须等待——等 A 输出完成并得到回复后，
      B 才能输出并等待回复（即 stdout/input 交互通道为互斥资源）。
    - 暂缓：Super 自主设计工作流程（Plan）、指定位置修改的 tool。

专家分区（待实现，之后再做）：
    - 每个专家独立线程/协程，无任务时挂起，有事件被 emit 唤醒（「常驻挂起」模型）。

控制台 IO 锁（已实现）：
    - Bus.io_print(text)：不带 input 的输出，只锁「写控制台」这一下（粒度最细，防多 Agent print 交错）。
    - Bus.io_dialog(text)：带 input 的对话回合，把「一次输出 + 下一次输入」作为完整过程锁定；
      等待输入时 await 挂起（不占用事件循环），返回用户输入。
    - 语义：输出和输入一一对应，带 input 的对话回合同一时刻只能有一个工具执行。

多轮交互式 Agent（TODO，下一步）：
    - 把「Agent（专家/Super）的整轮对话」作为一个重任务挂在 pending 上（走 slow_task 机制），
      支持 Agent 在 run_agent 中途「输出 → 等输入 → 继续」的多轮交互，而非一次性跑完。
    - 下一步关键动作：把 expert 放入 slow_task 运行（通过 bus.submit 挂到 pending，
      使其占用 IO 锁、等待输入时挂起，完成后从 pending 移除）。
    - 注意：当前工具式子 Agent（被动被 Super 路由调用）已够用，暂不做「常驻挂起」主动模型。

 当前未实现功能（登记，做了就划掉，新发现的及时补进来）：
     - 联网功能（目标栏已列，尚未实现）
     - Listener 音频听写：音频 API 监听、课堂内容记录（未实现）
     - 流式 IO 支持（未实现）
     - Super 自主 Plan（有向图流程设计）、指定位置修改的 tool（暂缓）
     - 鉴权系统：当前所有 Agent 共享 bus.tools 全量 schema，EXPERTS 中的工具子集实际未生效
     - 常驻挂起专家模型：Agent_core.agent_exec 仅占位，bus.receive 未实现；
       专家目前只能被 Super 以「慢任务工具」形式被动调用
     - Message.reply 回拨字段接线；emit/subscribe 的 deliver 循环当前未在任何入口启动（死代码）
     - 慢任务兜底：max_step 内未完成的任务结果会丢失（慢任务硬超时、「等待不烧 step」均未实现）
     - 对话复盘自动固化为 Tool 的流程（目前仅提示词约定 + add_memory）
     - 消息历史 token 截断 message_cutter（Agent.py 已注释，函数本体已不存在）
     - tmp.py 的 recognize_doc 重构（资源管理/页标题/去重/5页硬截断）未并入正式 Visal.py
     - PassageWrite「总结对话」缺数据通路：专家每次新建 messages，拿不到 Super 的对话历史
     - terminal.py 已损坏，待修复或删除（见隐患 5）

 已知隐患（登记，修复后划掉）：
     1.【高】super.py main() 中 add_memory / retrieve_context 被 add_tool 两次（约 142-143 与 159-160 行），
       schema 出现同名工具两份。
     2.【高】专家工具互相可见、可递归调用；专家慢任务持有 bus.semaphore 名额，其内部工具调用再竞争
       同一把锁：并行专家数 ≥ max_concurrency(4) 时死锁。
     3.【高】event_bus._ans_executer 双层包装：异常先包成 Message(Error)，外层又包 Message('Done', ...)，
       快任务错误永远显示 Done，Agent 无法区分 Done/Error。
     4.【高】Agent.run_agent 中 json.loads(tool_call.arguments) 无保护，LLM 返回非法 JSON 直接崩溃。
     5.【高】terminal.py 已坏：from Agent import agent（已不存在）；Tools.registry 装饰器内
       def decorater(func: function) 的 function 未定义 → NameError，import 即崩。
     6.【高】慢任务超限结果丢失，且残留 pending 带入下一轮对话。
     7.【中】recognize_doc 未强制 ≤5 页上限（仅 docstring 声称），LLM 传大页码列表可耗尽资源。
     8.【中】Saver.add_memory 注释声称重复 ID 自动覆盖，ChromaDB 重复 add 实际可能报
       DuplicateIDError（应改 upsert），未验证。
     9.【中】task_results 中 Submitted 后无人 poll 的任务永久残留（内存泄漏）；poll 取走即销毁，
       二次 poll 返回误导性 Submitted。
    10.【中】CLAUDE.md 称 PDF 页并发 OCR，实际串行；Pix2Text 单实例经 to_thread 多线程调用的
       线程安全性未验证。
    11.【低】Super 对话历史无截断，长对话有爆 token 风险。
    12.【低】OCR 结果的 Success 是字符串 'True'/'False' 非 bool；'inline formular' 拼写错误
       已成接口约定（缓存文件/测试均依赖），改名需同步迁移旧缓存。
    13.【低】Agent.py 调试残留 print(TASKKKS...)；慢任务等待固定 sleep(10) 且烧 step。
    14.【低】super.py 直接读 os.environ["DSH_OPENAI_KEY"]，缺失时 KeyError 无友好提示。
    15.【低】硬编码：Saver 嵌入模型绝对路径、base_url、模型名（换机器必须改）。
    16.【低】trail.py 是错误用法的忙循环实验代码（to_thread 传协程 + 无 await），运行即占满 CPU，勿运行。