import time
import os
import asyncio

ESC = '\x1b'

class LineExpress:
    '''渲染一行的内容，需要手动启用'''
    def __init__(self,line_number:int,name:str):
        '''初始化行渲染，必须提供行号'''
        self.content = ''
        self.line_number = line_number
        self.name = name

    async def flush(self,IOlock:asyncio.Semaphore):
        async with IOlock:
            print(f'{ESC}[s{ESC}[{self.line_number};0H{ESC}[2K{self.content}{ESC}[u')


    async def rewrite(self,content:str,):
        self.content = content
        await self.flush()

line_number = 2
content = 'result'
print("test\ntest\ntest\n",end='')
print(f'{ESC}[2;0H{ESC}2K',end='')
# print(f'{ESC}[s{ESC}[{line_number};0H{ESC}[2K{content}{ESC}[u')