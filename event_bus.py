import time,os
from pydantic import BaseModel, Field, create_model
from typing import Optional,Annotated,get_origin,Any,Pattern
from enum import Enum
from Tools import Tools
from Saver import Saver
import asyncio
import queue

tool = Tools()

class EOW(Exception):
    pass

async def executer(bus:Bus,func_name:str,handler = None,*args,**kwargs):
    '''执行函数功能的工具'''
    result = tool.execute(func_name=func_name,*args,**kwargs)
    # result = await tool.async_execute(func_name=func_name,*args,**kwargs)       #Tools内置保护，不用过设计  
    if handler == None:
        handler = func_name                                             #默认使用工具注册时的函数名作为handler
    bus.emit(Message(title=handler,content=result))
    return None

class Message():
    '''一个完整的消息字段，包含了发出者(title)内容(content)，未来可以扩充'''
    def __init__(self,title:str = None,content:Any = None):
        self.content:Any = content
        self.title:str = title
        return None

    def msg_set(self,title:str,content:Any):
        self.title = title
        self.content = content
        return None

class Bus():
    EOWM  = Message('Task_end')         #End Of Work Mark
    def __init__(self):
        self.handler = {} #handler注册队列
        self.message_queue = queue.Queue(-1)     #消息队列，长度不设限

    def registry(self,event_name:str):          #事件注册
        if event_name not in self.handler:
            self.handler[event_name] = set()
        return None
        # else:
        #     raise ValueError("事件名重复注册")  #之后可能改为return

    def subscribe(self,func_name:str,event_name:str) -> bool:
        self.registry(event_name=event_name)                    #自动订阅有误操作事件名导致信息丢失的风险，可以按情况启用
            # raise ValueError("未定义事件无法订阅")
        self.handler[event_name].add(func_name)
        return True

    def unsubscribe(self,func_name:str,event_name:str) -> bool:
        self.registry(event_name=event_name)                    #自动订阅有误操作事件名导致信息丢失的风险，可以按情况启用
            # raise ValueError("未定义事件无法取消订阅")
        if func_name not in self.handler[event_name]:
            return False                                                #取消失败没有风险，不会损坏流程，不抛出错误
            # raise ValueError("未订阅函数无法取消订阅")
        self.handler[event_name].remove(func_name)
        return True

    def emit(self,event_name:str,content:Any):                  #事件添加
        self.registry(event_name=event_name)
        self.message_queue.put(Message(title=event_name,content=content))
        return None

    def exception_submit(self,error_list):
        '''异常提交程序，需要配合外部函数规定的提交接口提交'''
        pass

    async def deliver(self):                        #由于最终程序会异步化，所以在这里声明中加入了async但我不知道是否有必要
        while True:
            error_list = {}
            msg = self.message_queue.get()
            if msg is self.EOWM:
                break
            title = msg.title
            for obj in self.handler[title]:
                try:
                    executer(self,obj,msg.content)           #最终由封装过的异步执行器自主判断并执行
                except Exception as e:
                    error_list[obj] = str(e)
            self.exception_submit(error_list)
        while not self.message_queue.empty():
            self.message_queue.get()                    #退出前清空缓存的内容
        raise EOW("Work is down")

    def _end(self):                                     #终止字符注入工具
        self.message_queue.put(self.EOWM)
        return None