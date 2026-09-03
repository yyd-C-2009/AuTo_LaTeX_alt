'''
目标: 提供任务管理基站, 依据 Passport 发放, 推进, 审查任务内容, 同时进行鉴权工作

同时自动进行任务推进, 不断自动分发任务, 查看任务进程
'''

from event_bus import Bus

class Task:
    '''子任务生命周期管理器, 维护对应任务的上下文以及护照信息'''
    def __init__(self):
        self.content:Content = Content()

class Content:
    '''
    子任务的上下文管理器, 包含了这个调用的生命周期的所有注入性信息
    过程中, 推荐的工具以及计划完成项目是会动态变化的, 预置上下文无法改写
    其中tool_recommended会根据Passport的调整动态变化
    plan会在离开计划这一步骤时自动更新
    同时会维持一个pending列表记录当前任务的在途慢任务
    这个功能将会逐步取代之前位于Agent.py中的pending
    '''
    def __init__(self,id,content,holder):
        self.task_id:int = id
        self.task_content:str = content
        self.holder:str = holder
        self.plan:str = None
        self.task_done:list = []
        self.tool_recommended:list = []
        return

    def __repr__(self):
        tmp_dict = {}
        tmp_dict['Goal'] = self.task_content
        tmp_dict['Your job'] = self.holder
        tmp_dict['Your present work'] = self.task_done
        if self.plan is not None:
            tmp_dict['Plan for the job'] = self.plan

        return str(tmp_dict)

    def addition(self,your_task:str):
        self.task_done.append(your_task)
        return

    def inspect(self):
        '''内省功能, 返回一个由当前上下文组成的Dict, 包含计划, 工作, 目标, 工作日志'''
        tmp_dict = {}
        # TODO
        return tmp_dict

class WorkCapability:
    '''保存某一个工作状态下的工具列表, 通过单独的配置文件引入, 进行注册表载入鉴权以及推荐目录维护'''
    def __init__(self):
        self.suggueted_tool:list = None
        self.normal_tool:list = None
        return

    def set_suggest(self,suggested:list):
        self.suggueted_tool = suggested
        return 

    def set_normal(self,normal:list):
        self.normal_tool = normal
        return

    def add_suggest(self,addition):
        self.suggueted_tool.append(addition)
        return

    def add_normal(self,addition):
        self.normal_tool.append(addition)
        return

    def tool_list(self,content:Content,bus:Bus) -> list:
        tmp_list = []
        content.tool_recommended = self.suggueted_tool
        for name in self.suggueted_tool:
            bus.tools.find_tool(name=name,tool_list=tmp_list)
        for name in self.normal_tool:
            bus.tools.find_tool(name=name,tool_list=tmp_list)
        return tmp_list

class WorkFlow():
    '''完整工作流管理器, 通过慢任务形式派发 Agent 工作, 通过 Task 完成任务推进, 进行运行鉴权触发'''
    def __init__(self):
        pass 