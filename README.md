# single_agent_trail
an trail for single agent

问题：
tool_call_id是独一无二的吗？他的生成机制是什么？

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


程序结构：
    Agent cyc waiting for message
    Agent_1->Bus->Agent_2

    Agent_exe->tool

消息总线：
    将所有模块间的消息通过总线传递
    消息总线上挂载的executer负责将msg解包为args并通过对应的tool.executer执行

    这里有一个问题：executer执行之后，对方不知道自己因该在哪里播报自己的结果，但是这不是问题，我们把一个约定的字段设为recall来表示回拨路径