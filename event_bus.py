import time,os
from pydantic import BaseModel, Field, create_model
from typing import Optional,Annotated,get_origin,Any,Pattern
from enum import Enum
from Tools import Tools
from Saver import Saver
import asyncio
import queue

class EOW(Exception):
    pass

async def executer(tool_list:Tools,func_name:str,msg:Message):
    '''执行函数功能的工具，Tools中的总线回传尚未完成'''
    # print(f'{func_name} 以 {msg.content} 执行')
    # 通信采用总线，模块间不再需要回传，所有通信统一使用Message
    result = await tool_list.async_execute(func_name=func_name,**msg.content)       #Tools内置保护，不用过设计  
    # if handler == None:
    #     handler = func_name                                             #默认使用工具注册时的函数名作为handler
    return result

class Message():
    '''一个完整的消息字段，包含了发出者(title)内容(content)，未来可以扩充'''
    def __init__(self,title:str = None,content:Any = None):
        self.content:dict = content                                  #强制返回字典
        self.title:str = title
        self.recall:str = None                                      #回拨地址，为空则在自己的频道广播
        return None

    def msg_set(self,title:str,content:Any):
        self.title = title
        self.content = content
        return None

class Bus():
    EOWM  = Message('Task_end')         #End Of Work Mark
    def __init__(self,tools : Tools = None):
        self.handler = {} #handler注册队列
        self.message_queue = asyncio.Queue(-1)     #消息队列，长度不设限
        self.tools = tools if tools is not None else Tools()
        # print(f'testttt{tools.tool_list}')

    def registry(self,event_name:str):          #事件注册
        if event_name not in self.handler:
            self.handler[event_name] = []
        return None
        # else:
        #     raise ValueError("事件名重复注册")  #之后可能改为return

    def subscribe(self,func_name:str,event_name:str) -> bool:
        self.registry(event_name=event_name)                    #自动订阅有误操作事件名导致信息丢失的风险，可以按情况启用
            # raise ValueError("未定义事件无法订阅")
        if func_name not in self.handler[event_name]:
            self.handler[event_name].append(func_name)
        # print(f'{func_name} 订阅了 {event_name} 事件')
        return True

    def unsubscribe(self,func_name:str,event_name:str) -> bool:
        self.registry(event_name=event_name)                    #自动订阅有误操作事件名导致信息丢失的风险，可以按情况启用
            # raise ValueError("未定义事件无法取消订阅")
        if func_name not in self.handler[event_name]:
            return False                                                #取消失败没有风险，不会损坏流程，不抛出错误
            # raise ValueError("未订阅函数无法取消订阅")
        self.handler[event_name].remove(func_name)
        return True

    def emit(self,event_name:str,content:dict = None):                  #事件添加
        self.registry(event_name=event_name)
        self.message_queue.put_nowait(Message(title=event_name,content=content))
        return None

    def exception_submit(self,error_list):
        '''异常提交程序，需要配合外部函数规定的提交接口提交'''
        pass

    async def deliver(self):                        #由于最终程序会异步化，所以在这里声明中加入了async但我不知道是否有必要
        while True:
            error_list = {}
            msg = await self.message_queue.get()
            if msg is self.EOWM:
                break
            # print(f'test{msg.content}')
            title = msg.title
            tasks = [asyncio.create_task(executer(tool_list=self.tools,func_name=obj,msg=msg)) for obj in self.handler[title]]           #最终由封装过的异步执行器自主判断并执行
            if tasks:
                result = await asyncio.gather(*tasks,return_exceptions=True)
                for e,name in zip(result,self.handler[title]):           # handler跨线程安全问题
                    if isinstance(e,Exception):
                        error_list[name] = e
            self.exception_submit(error_list)
        while not self.message_queue.empty():
            self.message_queue.get_nowait()                    #退出前清空缓存的内容
        return None

    def _end(self):                                     #终止字符注入工具
        self.message_queue.put_nowait(self.EOWM)
        return None