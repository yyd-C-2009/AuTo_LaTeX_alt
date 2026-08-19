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

    文件编辑工具 str_replace_editor（view/create/str_replace/insert，路径限制在项目目录内）

    .gitattributes（* text=auto + * text eol=lf）：统一所有文本文件行尾为 LF，
    .png/.pdf 标记为二进制不做转换，减少跨平台协作时的行尾杂音。

    联网工具 built_in_tool.py：web_search（DuckDuckGo HTML 端点，无需 API key）、
    web_fetch（抓取网页转纯文本）、get_weather（wttr.in 实时天气）。
    均同步阻塞，经 Tools.async_execute 走 to_thread；已在 super.py main() 注册
    （web_search/web_fetch time_out=60，get_weather time_out=15）。

    LaTeX 语法检查工具 check_latex（super.py）：用 pdflatex -draftmode 编译 latex_output/ 下的
    .tex 文件，只做语法检查、不生成 PDF；已加入 mathwrite / passagewrite 的 tool_names。

    Super 提示词查看工具 view_expert_prompts（super.py）：返回 EXPERTS 中每位专家的系统提示词
    与工具白名单；已注册为全局工具（time_out=5）。Super 复盘诊断提示词时可调用；
    专家因 tool_names 白名单不可见此工具，也不会越权调用。

    定理环境规范查看工具 view_theorem_style（super.py）：读取项目根目录 example.tex 全文并返回，
    作为 LaTeX 定理环境与 label/ref 命令的唯一规范来源；已注册为全局工具（time_out=5），
    并加入 mathwrite / passagewrite 的 tool_names。

    terminal.py 多Agent直接对话终端（重置版）：支持 /db 查看数据库、/agent 切换直接对话身份、
    /branch 对话分支（list/fork/new/switch/rm）、/cd 切换工作目录、/pwd 查看工作目录、
    /whoami、/help、/exit；复用 super.py 的 register_common_tools / build_client / build_super_tools。

    Setup.py 一键安装脚本：pip 走清华/阿里云/中科大镜像，HuggingFace 模型走 hf-mirror.com；
    默认安装依赖并下载 BGE 嵌入模型，--with-ocr 可预下载 Pix2Text OCR 模型，
    --with-listener 可预下载 Faster-Whisper 模型（--listener-source modelscope/hf，默认 modelscope 国内源）。

    Listener 音频转写与连续监听（listener.py）：Listener 类在 init() 中常驻加载 Faster-Whisper，
    transcribe_audio 转写音频文件，start_listening/stop_listening/get_listen_result 连续监听；
    已注册为全局工具并加入 listener 专家白名单。

    清理：已删除所有 test_*.py 以及 tmp.py、trail.py（实验/损坏代码）；
    历史条目中「验证：python test_*.py」为过往修复记录，对应 test 文件已被清理。

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
     - ✅已实现：联网功能——built_in_tool.py 提供 web_search / web_fetch / get_weather（见「已实现功能」）。
     - ✅已实现：Listener 音频听写——Listener 类常驻 Faster-Whisper 模型，提供 transcribe_audio、
       start_listening/stop_listening/get_listen_result 连续监听；EXPERTS 已加入 listener，
       Setup.py 可 --with-listener 预下载模型，sounddevice 麦克风依赖已加入 Setup.py。
     - 流式 IO 支持（未实现）
     - ✅已实现：LaTeX 语法检查工具 check_latex（只做语法检查，不生成 PDF）：
       用 pdflatex -draftmode 编译 latex_output/ 下的 .tex 文件，
       已加入 mathwrite / passagewrite 的 tool_names（「可写文件」专家），未加入 Math（不可见）。
     - ✅已实现：「查看其他 Agent 提示词」工具 view_expert_prompts（见「已实现功能」）。
       配合 SUPER_PROMPT 新增的
       "同时，诊断当前对于各个专家的提示词是否合理，是否需要调整。"
       Super 可随时调用 view_expert_prompts 读取 EXPERTS 里各专家的系统提示词与工具白名单，
       便于 Super 在复盘时对比判断各专家提示词是否合理、需要调整。
     - LaTeX-label 管理工具：供 passage_editer(PassageWrite) 查看当前文档中公式的编号情况
       （如 \label / \eqref / 编号是否缺失、是否重复），帮助纠正「不爱给公式编号」的习惯。
     - 提示词打磨：目前专家过多用中文解释问题，缺少严谨的论述过程；需要修改各专家提示词，
       引导专家输出严谨、有推导过程的论述，而非中文口语化解释。
     - 自定义指令工具（数个 tool，部分已实现）：后续 LaTeX 编辑会在一些「已定义的自定义指令」前提下进行，
       需要提供若干工具让专家认识、查询这些已定义的自定义指令（及其语义/用法），避免误用或重复造轮子。
       已实现 view_theorem_style（定理环境与 label/ref 规范，见「已实现功能」）；
       其余自定义指令查询工具待补。
     - Super 自主 Plan（有向图流程设计）、指定位置修改的 tool（暂缓）
     - 鉴权系统（执行层白名单，已实现）：Agent.run_agent 通过 tool_names 施加「执行层白名单」——
       schema 级过滤只让专家「看不见」，执行层校验才真正阻止越权提交，杜绝专家自我调用/互相甩锅；
       Super 不传 tool_names（None）保留全量调度权。EXPERTS 的工具子集已真正生效。
     - 常驻挂起专家模型：Agent_core.agent_exec 仅占位，bus.receive 未实现；
       专家目前只能被 Super 以「慢任务工具」形式被动调用
     - Message.reply 回拨字段接线；emit/subscribe 的 deliver 循环当前未在任何入口启动（死代码）
     - 慢任务兜底：max_step 内未完成的任务结果会丢失（慢任务硬超时、「等待不烧 step」均未实现）
     - 对话复盘自动固化为 Tool 的流程（目前仅提示词约定 + add_memory）
     - 消息历史 token 截断 message_cutter（Agent.py 已注释，函数本体已不存在）
     - tmp.py 的 recognize_doc 重构（资源管理/页标题/去重/5页硬截断）未并入正式 Visal.py
       （注：tmp.py 已删除，此重构未发生，本条目留作历史，正式 Visal.py 仍是当前实现）
     - ✅已实现：PassageWrite「总结对话」数据通路——build_super_tools 接收 Super 的 messages 引用，
       专家被调用时用 _dialogue_context 提取「用户 ↔ Super」纯文本对话注入上下文
       （过滤 role:tool/空 content，limit 截断；验证：python test_dialogue_path_demo.py）
     - ✅已实现：terminal.py 已重置为多Agent直接对话终端（见「已实现功能」），
       提供 /db 查看数据库、/agent 切换身份、/branch 对话分支等系统指令。

 MathWrite 定理环境编写规范（依据 example.tex，已实施）：
   - 新增只读工具 view_theorem_style（super.py）：读取项目根目录 example.tex 全文并返回，
     已注册为全局工具（time_out=5），并加入 mathwrite / passagewrite 的 tool_names。
     Super 全局可见；mathwrite / passagewrite 按白名单可见；math / draw 不可见。
   - 已更新 EXPERTS["mathwrite"] 与 EXPERTS["passagewrite"] 提示词：
     写/改 .tex 前必须先调用 view_theorem_style 获取规范，导言区与定理环境必须按
     example.tex 的约定执行，写完后必须调用 check_latex 直到通过。

   规范要点（以 example.tex 原文为准）：
     - 导言区：`\documentclass{book}`；宏包 inputenc / xeCJK / amsmath,amsthm,amssymb /
       graphicx / booktabs / enumitem / hyperref / bm / darkmode 按需保留；
     - 定理环境声明：
       `\newtheorem{theorem}{Theorem}[section]`、
       `\newtheorem{lemma}[theorem]{Lemma}`、
       `\newtheorem{proposition}[theorem]{Proposition}`、
       `\newtheorem{corollary}[theorem]{Corollary}`、
       `\newtheorem{definition}[theorem]{Definition}`、
       `\newtheorem{remark}{Remark}[section]`、
       `\newtheorem{Example}{Example}[section]`、
       `\newenvironment{solution}{\begin{proof}[Solution]}{\end{proof}}`；
     - 标签命令：`\theolabel{key}` / `\lemmlabel{key}` / `\proplabel{key}` /
       `\corolabel{key}` / `\deflabel{key}` / `\exaplabel{key}`；
     - 引用命令：`\theoref{theo:key}` / `\lemmref{lem:key}` / `\propref{prop:key}` /
       `\cororef{coro:key}` / `\defref{def:key}` / `\exapref{exap:key}`；
       注意 ref 参数是「带前缀的完整标签」（如 `\defref{def:something}`）。
     - proof 不编号；solution 环境等价于 proof 的 Solution 标题；
       行间公式继续使用 `\label` 编号，引用用 `\eqref`。

   验收方式：
     - mathwrite / passagewrite 输出文件中不得自行编写与 example.tex 冲突的 `\newtheorem`
       或 label 前缀；定理类环境共享 theorem 计数器，remark/Example 独立按 section 计数；
     - 每次写完/改完 .tex 后必须调用 check_latex，直到返回「语法检查通过」。

 Listener 音频转写（已实施，Faster-Whisper small/int8）：
   目标：新增 Listener 专家/工具，将课堂音频（文件或麦克风）转写为文字，
   并把转写结果交给 Super/PassageWrite 做课堂记录。

   模型选型（已确认：Faster-Whisper small/int8/CPU）：
     A. Faster-Whisper（推荐）：faster-whisper + CTranslate2，CPU 可跑，支持中英、
        时间戳、VAD。模型走 HF 镜像下载：tiny/base/small/medium/large-v3；
        中文课堂建议 small（约 484MB，均衡）或 medium（约 1.5GB，更准）。
     B. OpenAI Whisper（openai-whisper + PyTorch）：生态成熟，但 CPU 慢、内存高；
        不优先推荐。
     C. FunASR（阿里，ModelScope）：中文 ASR 与标点最强，但依赖更重；
        适合对中文准确率要求高的场景。
     D. 云 API（OpenAI/讯飞等）：无需本地模型，但需网络与密钥；可作为后续扩展。

   实施内容（已完成）：
     1. listener.py 重构为 Listener 类，与 Visal 相同的长生命周期模式：
        在 initer.init() 中创建一次，Faster-Whisper 模型加载后常驻内存，不重复卸载；
        register_common_tools 注册该实例的 bound methods。
     2. 单文件转写 transcribe_audio(audio_path, language="auto", task="transcribe") -> str，
        返回带时间戳文本；模型目录支持 LISTENER_MODEL_DIR，默认 ./models/faster-whisper-small；
        目录不存在时不再自动联网下载（避免 HF 网络超时拖死启动），而是记录 load_error
        并提示运行 Setup.py --with-listener --listener-source modelscope 下载。
     3. 连续监听（依赖 sounddevice）：
         - start_listening(segment_duration=8.0, sample_rate=16000)：后台线程启动麦克风流，
           按段转写并累积到内存；
         - get_listen_result(include_timestamps=True)：只读查看当前累积转写，不会取走/清空内容；
         - stop_listening(include_timestamps=True)：停止监听并返回累积转写（不清空）；
         - clear_listen_result(confirm=True)：显式清空累积转写。
     4. Setup.py 已加入 faster-whisper / sounddevice / numpy 依赖，并新增
        --with-listener / --listener-size / --listener-source 参数；默认从 ModelScope
        下载 pengzhendong/faster-whisper-<size> 到 ./models/faster-whisper-<size>，
        --listener-source hf 则走 hf-mirror.com。
     5. EXPERTS 已增加 "listener"：tool_names=["transcribe_audio", "start_listening",
        "stop_listening", "get_listen_result", "clear_listen_result"]；
        build_super_tools 自动注册 listener_expert，且 listener_expert 超时放宽到 1800s。
     6. 数据通路：Super/PassageWrite 可调用 listener_expert 转写音频/连续监听，再由
        PassageWrite 总结为课堂记录；当前连续监听为「手动启动/停止」模式。
     7. 权限边界：连续监听有状态工具（start_listening / stop_listening / get_listen_result /
        clear_listen_result）只允许 listener_expert 通过白名单调用；Super 直接对话时通过
        run_agent 的 deny_tools 禁用这些工具，必须经 listener_expert 间接使用，避免多 Agent
        轮流读取/清空同一份累积转写造成状态竞争。transcribe_audio 仍对 Super 全局可见。
     8. 加载失败不致命：Listener.__init__ 若模型缺失/下载失败，不再抛出异常导致启动失败；
        会记录 load_error，transcribe_audio/start_listening 返回明确提示：
        python Setup.py --with-listener --listener-source modelscope 下载模型。

   后续可扩展（未实施）：
     - 实时 VAD 静音分段（当前按固定秒数分段）；
     - Listener 主动汇报：接 Bus.emit/subscribe（当前 deliver 循环仍是死代码，暂不依赖）。

 已知隐患（登记，修复后划掉）：
     1.【高】✅已修复（核对确认）：super.py 中 add_memory / retrieve_context 只 add_tool 一次（144-145 行），
       schema 无重复同名工具。
     2.【高】✅已处理：专家越权/递归调用由 Agent.run_agent 的执行层白名单阻止（见「鉴权系统」条目）；
       死锁由 bus.mark_reentrant（专家执行不占 semaphore）缓解。
     3.【高】✅已修复（核对确认）：_ans_executer 对 isinstance(result, Message) 的返回值原样返回，
       不再外包 Done，快任务错误能正确显示 Error。
     4.【高】✅已修复（核对确认）：json.loads 已包 try/except，解析失败回填错误 tool 消息让 LLM 自行修正。
     5.【低】✅已处理：terminal.py 已重置为多Agent直接对话终端（见「已实现功能」），
       旧版「独立入口脚本 + Agent.agent()」已移除；新版复用 super.py 的公共工具注册、
       client 构建与专家工具注册，支持 /db、/agent、/branch 等系统指令。
     6.【高】慢任务超限结果丢失，且残留 pending 带入下一轮对话。
     7.【中】✅已修复（核对确认）：Visal.py recognize_doc 中 pages = pages[:5] 硬截断为前 5 页。
     8.【中】✅已处理：Saver.add_memory 由 collection.add 改为 collection.upsert，
       重复 ID 真正覆盖，不再 DuplicateIDError（本机验证：python test_saver_upsert_demo.py）。
     9.【中】✅已处理：新增 _gc_task_results 惰性清理——已完成的慢任务结果超过 _task_ttl(600s)
       仍无人 poll 则自动销毁（submit/poll 时触发清理），解决内存泄漏；已取走再 poll 返回明确的 UnknownTaskId。
    10.【中】CLAUDE.md 称 PDF 页并发 OCR，实际串行；Pix2Text 单实例经 to_thread 多线程调用的
       线程安全性未验证。
    11.【低】Super 对话历史无截断，长对话有爆 token 风险。
    12.【低】OCR 结果的 Success 是字符串 'True'/'False' 非 bool；'inline formular' 拼写错误
       已成接口约定（缓存文件/测试均依赖），改名需同步迁移旧缓存。
    13.【低】Agent.py 调试残留 print(TASKKKS...)；慢任务等待固定 sleep(10) 且烧 step。
    14.【低】super.py 直接读 os.environ["DSH_OPENAI_KEY"]，缺失时 KeyError 无友好提示。
    15.【低】部分处理：Saver 嵌入模型路径已支持 BGE_MODEL_DIR 与项目内 ./models/bge-base-zh-v1.5
        （Setup.py 会下载到该目录）；base_url、模型名仍硬编码，换机器仍需改。
    16.【低】✅已清理：trail.py 已删除（原为错误用法的忙循环实验代码：to_thread 传协程 + 无 await）。
    17.【高】✅已处理（鉴权，新增问题）：Agent 会自己调用自己/其他专家互相甩锅——根因是
       schema 级过滤只能让 LLM「看不见」其他工具，但 bus.submit 执行时不校验调用者身份，
       专家一旦返回越权 tool_call 仍会被照单执行。已在 Agent.run_agent 增加执行层白名单校验：
       tool_names 非 None 时提交前校验 func_name 是否在允许集合内，越权则拒绝并回填错误消息。
       （验证：python test_authz_demo.py）
    18.【高】✅已处理（新工具踩坑，根因记录）：给 Agent 的工具函数【勿加】`from __future__ import annotations`。
       它会令 inspect.signature 拿到的注解变成字符串（如 "Optional[List[int]]"），
       Tools.function_to_model 不解析字符串，会把字符串当类型塞给 create_model，
       pydantic 报 PydanticUserError: ...you should define `Optional`...。
       已通过移除 future import + 用 PEP604 写法 `X | None` 替代 `Optional[X]` 修复（str_replace_editor.py）。
       （验证：python test_str_replace_editor_demo.py）
    19.【低】✅已处理：Bus.io_dialog 曾重复输出提示——内部既 print(text) 又 input("你: ")，
       导致调用 bus.io_dialog("你: ") 时屏幕出现两个「你: 」。已把 input 的内置提示符去掉，
       提示只由 print(text) 输出一次（提示文本由调用方传入）。
    20.【高】✅已处理（延迟结果窗口，根治 400 与重复提交）：
       根因：慢任务提交后先回填「占位 tool」消息（assistant->tool 配对），完成时又回填第二条同 tool_call_id 的
       tool 消息，违反 OpenAI「一条 role:tool 只能对应前面一条 assistant tool_calls」的规则 → 报 400
       (Messages with role 'tool' must be a response to a preceding message with 'tool_calls')，并诱发重复提交。
       方案：慢任务完成后不再往 messages 回填第二条 tool，而是把结果（含 function 函数名、query 提问内容、
       result 结果）存入每个 Agent 独立的 self.delayed_results 缓存；新增全局工具 view_delayed_results，
       LLM 主动调用时被 Agent.run_agent 拦截（不经 bus.submit/鉴权），强制注入当前 Agent 的缓存并返回，读后清空。
       慢任务等待改由 _wait_pending 轮询 poll，不消耗 max_step（避免 Super 提前放弃 Timeout）。
       （验证：python test_delayed_results_demo.py）

 本次修改记录（本次编辑新增）：
     - super.py 新增 view_expert_prompts 工具：返回 EXPERTS 中每位专家的系统提示词与工具白名单；
       已在 main() 注册（time_out=5），并更新 SUPER_PROMPT 提示 Super 在诊断提示词时可调用。
     - README 补充记录 built_in_tool.py / super.py 中已加入的 get_weather 天气工具（wttr.in，time_out=15）。
     - super.py 新增 view_theorem_style 工具：读取 example.tex 全文作为定理环境与 label/ref 规范，
       已在 main() 注册（time_out=5），并加入 mathwrite / passagewrite 的 tool_names。
     - 更新 EXPERTS["mathwrite"] / EXPERTS["passagewrite"] 提示词：写/改 .tex 前先调用
       view_theorem_style，严格按 example.tex 的导言区、定理环境声明与 label/ref 命令执行，
       写完必须 check_latex 直到通过。
     - README 将「MathWrite 定理环境编写计划」更新为「已实施」并补充规范要点与验收方式。
     - terminal.py 重置为多Agent直接对话终端：新增 /db（查看数据库）、/agent（切换直接对话 Agent）、
       /branch（对话分支 list/fork/new/switch/rm）、/whoami、/help、/exit 系统指令；
       切换 Agent 会清空当前分支历史，分支 fork/new/switch/rm 管理多线对话。
     - Saver.py 新增 list_memories()：读取 chromadb 中全部记忆（id/text/metadata），
       供 terminal.py 的 /db 命令使用，不注册为 Agent 工具。
     - super.py 抽出 register_common_tools() 与 build_client()，供 super.py 与 terminal.py 共用，
       避免两套 REPL 注册工具/创建 client 的逻辑漂移。
     - CLAUDE.md 同步更新 terminal.py 描述（直接对话多Agent终端）与 initer/Saver 相关说明。
     - 新增 Setup.py：pip 依赖安装（清华/阿里云/中科大镜像，自动切换）+ BGE 嵌入模型下载；
       --with-ocr 可预下载 Pix2Text OCR 模型，--skip-models 只装依赖。
     - Saver.py load_embedder 改为按「显式参数 > BGE_MODEL_DIR > ./models/bge-base-zh-v1.5 >
       旧机器路径」顺序查找，新机器无需改代码即可使用 Setup.py 下载的模型。
     - Visal.py 在导入 pix2text 前默认设置 HF_ENDPOINT=https://hf-mirror.com（不覆盖已有值）。
     - README 新增 Listener 实现方案，经确认后已实施（Faster-Whisper small/int8/CPU）。
     - 新增 listener.py：Faster-Whisper 本地音频转写工具 transcribe_audio；模型懒加载，
       默认 small/int8/CPU，支持 LISTENER_MODEL_DIR/LISTENER_MODEL_SIZE/HF_ENDPOINT 覆盖。
     - super.py EXPERTS 增加 listener 专家，SUPER_PROMPT 增加 listener_expert；
       register_common_tools 注册 transcribe_audio；listener_expert 超时 1800s。
     - Setup.py 增加 faster-whisper 依赖与 --with-listener/--listener-size 参数，
       可预下载 Systran/faster-whisper-small 到 ./models/faster-whisper-small。
     - terminal.py 新增 /cd <path> 切换工作目录、/pwd 查看当前工作目录。
     - listener.py 重构为 Listener 类，并接入 initer.init()：模型启动时加载一次、常驻内存；
        register_common_tools 改为注册实例方法。
     - 新增连续监听工具 start_listening / stop_listening / get_listen_result（sounddevice 麦克风，
       后台线程按段转写并累积）；Setup.py 增加 sounddevice/numpy 依赖。
     - get_listen_result 改为只读（不取走/不清空），新增 clear_listen_result(confirm=True) 显式清空。
     - Listener 加载时打印实际模型路径/来源（本地目录或 HF 缓存），便于确认下载位置。
     - Listener 模型加载失败改为非致命：启动不崩溃，工具调用时返回下载指引。
     - Setup.py 新增 --listener-source {modelscope,hf}，默认 modelscope 国内源
       （pengzhendong/faster-whisper-<size>），通过 ModelScope API 直接下载到本地模型目录；
       新增 --skip-packages 可只下载模型跳过 pip 安装。
     - Agent.run_agent 增加 deny_tools 参数：Super 直接对话时禁用连续监听有状态工具，
       只能通过 listener_expert 间接使用，避免多 Agent 轮流读取/清空同一份累积转写。
