'''
目标: 提供鉴权系统, 以及当前持有者的运行项目和自身固有身份, 提供权限变更对应的服务, 不接触上下文
'''

from enum import Enum
from Workflow import WorkCapability,Content,WorkFlow

class State(Enum):                  # passport state
    TOOL = 'tool'
    PLANNING = 'planning'
    WRITING = 'file_wrtiting'
    VERIFICATION = 'verification'
    WORKING = 'task dealing'
    DONE = 'task done'

STATE_LIST = [State.PLANNING,State.WORKING,State.WRITING,State.VERIFICATION,State.DONE]

class Passport:
    '''
    持有一个具体任务的身份信息, 不接触具体的业务逻辑, 只会提供鉴权所需要的信息
    '''
    def __init__(self,identical:str,task_id:int):
        self.state = State.PLANNING
        self.identical:str = identical
        self.task_id:int = task_id
        self.capability = WorkCapability()
        return

    def init_cap(self,capability_list:dict[str,str]):
        '''
        外部数据按照json格式注入, 需要按照Agent给出每一个阶段工具清单, 这个里面的工具原则上时写死的, 不允许随意改动
        避免Agent自发调整自己的权限/做出不必要的举动, 需要跨权限调用或者跨会话确认的时候, 通过 Super_call 向组织者发出
        申请/询问, 然后通过 Super_executer 跨权限执行工具
        '''
        self.capability[State.TOOL] = dict.get(State.TOOL.value)
        self.capability[State.PLANNING] = dict.get(State.PLANNING.value)
        self.capability[State.WRITING] = dict.get(State.WRITING.value)
        self.capability[State.VERIFICATION] = dict.get(State.VERIFICATION.value)
        self.capability[State.WORKING] = dict.get(State.WORKING.value)
        self.capability[State.DONE] = 'None'
        return

    def next_state(self,workflow:WorkFlow,if_checked:bool = True,):
        '''向下一个状态转移, 为了保证不错过环节, 只设置规定的向下移动'''
        if if_checked:
            self.state = STATE_LIST[STATE_LIST.index(self.state) + 1]
        else:
            self.redo()
        return

    def __repr__(self):
        tmp_dict = {}
        tmp_dict['STATE'] = self.state.value
        tmp_dict['IDENTICAL'] = self.identical
        return str(tmp_dict)
    
    def view_state(self):
        '''查看当前的进行状态, 需要与 WorkFlow 同步'''
        return self.__repr__()

    def redo(self):
        '''提供给verification鉴定工具的重做功能, 会根据鉴定工具的反馈将当前 Passport 重置到 Planning 状态'''
        # TODO
        return