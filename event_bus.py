import time,os
from pydantic import BaseModel, Field, create_model
from typing import Optional,Annotated,get_origin,Any,Pattern
from enum import Enum
from Tools import Tools
from Saver import Saver
import asyncio
import queue

def executer(func_name:str,*args,**kwargs):
    '''执行函数功能的工具'''
    pass

class Message():
    '''一个完整的消息字段，包含了发出者(title)内容(content)，未来可以扩充'''
    def __init__(self,title:str = None,content:str = None):
        self.content:str = content
        self.title:str = title
        return None

    def msg_set(self,title:str,content:str):
        self.title = title
        self.content = content
        return None

class Bus():
    def __init__(self):
        self.handler = {str,[str]} #handler注册队列
        self.message_queue = []     

    def registry(self,event_name:str):
        if event_name not in self.handler:
            self.handler[event_name] = []
            return None
        else:
            raise ValueError("事件名重复注册")

    def subscribe(self,func_name:str,event_name:str) -> bool:
        if event_name not in self.handler:
            raise ValueError("未定义事件无法订阅")
        self.handler[event_name].append(func_name)
        return True

    def emit(self,event_name):
        self.message_queue.append(event_name)