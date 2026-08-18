from typing import Any
from Tools import Tools
import asyncio

class _NEF():                #Not Ended Formation
    pass

NEF = _NEF()                 #全局唯一的未结束标记，所有未结束的任务都返回这个标记，直到任务完成后才会返回结果

class Message():
    '''一个完整的消息字段，包含了发出者(title)内容(content)，未来可以扩充'''
    def __init__(self,title:str = None,content:Any = None):
        self.content:dict = content                                  #强制返回字典
        self.title:str = title
        self.reply:str = None                                      #回拨地址，为空则在自己的频道广播
        if title not in ['Submitted','Done','Error','Event','Task_end']:               #直接报错，所有的消息格式的明文内容都是人手工填写的，机器只能选择，不能自定义能容，方便调试与改错
            raise ValueError('错误的消息类型')
        return None

    def msg_set(self,title:str,content:Any):
        self.title = title
        self.content = content
        return None

    def __repr__(self):
        return str({'title':self.title,'content':str(self.content)})

class Bus():
    '''
    总线，负责消息的分发和订阅，所有模块间通信都通过总线进行，所有模块都可以订阅和发布消息。所有返回值均为Message格式
    外部函数从tools.async_execute接入系统
    '''
    EOWM  = Message('Task_end')         #End Of Work Mark
    def __init__(self,tools : Tools = None,max_concurrency:int = 5):
        self.handler = {} #handler注册队列
        self.message_queue = asyncio.Queue(-1)     #消息队列，长度不设限
        self.tools = tools if tools is not None else Tools()
        self.semaphore = asyncio.Semaphore(max_concurrency)        #并发数限制，默认5个
        self.max_concurrent_tasks = max_concurrency
        self.fail_task = []
        self.metrics={                          #信息统计
            'emitted' : 0,
            'delivered' : 0,
            'failed' : 0,
        }
        #重任务管理
        self.task_results = {}                    # task_id -> 结果（None=处理中）
        self.task_counter = 0                     # 自增 id 生成器
        self.slow_tasks = set()                   # 走 submit 的慢任务名集合
        self.reentrant_tasks = set()              # 可重入慢任务名集合（内部会再 submit，执行时不占用 semaphore）
        # self._released_tasks = set()            # TODO: 高级特性——放弃任务集合，暂注释
        # —— 控制台 IO 锁（多 Agent 共享控制台时串行化输出/对话）——
        self._io_lock = asyncio.Lock()            # 带 input 的对话回合锁
        # print(f'testttt{tools.tool_list}')

    def registry(self,event_name:str):          #事件注册
        if event_name not in self.handler:
            self.handler[event_name] = []
        return None

    def subscribe(self,func_name:str,event_name:str) -> bool:
        self.registry(event_name=event_name)                    #自动订阅有误操作事件名导致信息丢失的风险，可以按情况启用
        if func_name not in self.handler[event_name]:
            self.handler[event_name].append(func_name)
        # print(f'{func_name} 订阅了 {event_name} 事件')
        return True

    def unsubscribe(self,func_name:str,event_name:str) -> bool:
        self.registry(event_name=event_name)                    #自动订阅有误操作事件名导致信息丢失的风险，可以按情况启用
        if func_name not in self.handler[event_name]:
            return False                                                #取消失败没有风险，不会损坏流程，不抛出错误
        self.handler[event_name].remove(func_name)
        return True

    def mark_slow(self,tasks:list[str] = []):
        self.slow_tasks.update(tasks)
        return None

    def mark_reentrant(self,tasks:list[str] = []):
        '''标记「内部还会再调用 submit」的慢任务（如专家）：执行时不占用并发名额，避免「持锁等锁」死锁'''
        self.reentrant_tasks.update(tasks)
        return None

    def emit(self,event_name:str,content:dict = None):                  #事件添加
        '''广播一个事件，除非实现函数内有约定，不会返回具体值'''
        self.registry(event_name=event_name)
        self.message_queue.put_nowait(Message(title='Event',content={'event_name':event_name,'args':content or {}}))
        self.metrics['emitted'] += 1
        return None

    def _exception_submit(self,error_list : dict):
        '''异常提交程序，需要配合外部函数规定的提交接口提交，目前只在总线留底，没有进行处理'''
        if not error_list:
            return
        for task,error in error_list.items():
            self.fail_task.append({task:error})
            self.metrics['failed'] += 1

    def _ensure_semaphore(self):
        '''检查并发限制器是否工作'''
        if self.semaphore is None:
            self.semaphore = asyncio.Semaphore(self.max_concurrent_tasks)

    async def deliver(self):                        
        '''分布式派发消息'''
        self._ensure_semaphore()
        while True:
            msg = await self.message_queue.get()
            if msg is self.EOWM:
                break
            # print(f'test{msg.content}')
            title = msg.content.get('event_name',None)
            if title is None:
                self.metrics['failed'] += 1
                continue
            for task in self.handler[title] :
                self.metrics['delivered'] += 1
                asyncio.create_task(self._executer(
                    func_name=task,
                    **msg.content['args'],
                ))         #最终由封装过的异步执行器自主判断并执行
        while not self.message_queue.empty():
            self.message_queue.get_nowait()                    #退出前清空缓存的内容
        return None

    def _end(self):                                     #终止字符注入工具
        '''注入终止字符'''
        self.message_queue.put_nowait(self.EOWM)
        return None

    # ==================== 控制台 IO 锁（多 Agent 共享控制台） ====================

    async def io_print(self, text: str = ""):
        '''不带 input 的输出：只锁「写控制台」这一下（粒度最细，防止多 Agent print 交错）'''
        async with self._io_lock:
            print(text)

    async def io_dialog(self, text: str = "") -> str:
        '''带 input 的对话回合：把「一次输出 + 下一次输入」作为完整过程锁定。
        等待输入时 await 挂起（不占用事件循环），返回用户输入内容。'''
        async with self._io_lock:
            print(text)
            return await asyncio.to_thread(input, "你: ")

    # ========================================================================


    async def submit(self,func_name:str,**kwargs):
        '''外部启动单个功能入口,返回一个Message，若为慢任务则返回一个id，若为快任务则返回结果'''
        if func_name in self.slow_tasks:
            self.task_counter += 1
            id = self.task_counter
            self.task_results[id] = NEF
            asyncio.create_task(self._run_slow(id = id,func_name=func_name,**kwargs))
            return Message(title = 'Submitted',content = {'id' : id} )

        result = await self._ans_executer(func_name=func_name,**kwargs)
        return result

    async def submit_coro(self, coro):
        '''提交任意协程（如 Agent 的 run_agent）作为慢任务：
        挂到 pending/task_results，后台执行，完成后结果写入 task_results。
        返回 Submitted 消息（含 task_id），供后续 poll 回收结果。'''
        self.task_counter += 1
        id = self.task_counter
        self.task_results[id] = NEF
        asyncio.create_task(self._run_coro(id=id, coro=coro))
        return Message(title='Submitted', content={'id': id})

    async def _run_coro(self, id: int, coro):
        '''后台执行任意协程慢任务，完成后把返回值写入 task_results'''
        try:
            result = await coro
            self.task_results[id] = Message(title='Done', content=result)
        except Exception as e:
            self.task_results[id] = Message(
                **{'title': 'Error', 'content': {'error': str(e), 'error_type': type(e).__name__}}
            )
            self._exception_submit({f"coro_{id}": str(e)})
        return None

    def poll(self,id : int): 
        '''查询慢任务结果：处理中→Submitted；完成→原结果并销毁；未知/已取走→Error（避免误导性重查）'''
        if id not in self.task_results:
            return Message(**{'title':'Error','content':{'error':f'task_id {id} 不存在（从未提交或结果已被取走）','error_type':'UnknownTaskId'}})
        result = self.task_results.get(id,NEF)
        if result is NEF:
            return Message(title='Submitted',content={'id' : id})
        self.task_results.pop(id)                                   #取走即刻销毁
        return result

    async def request(self, event_name: str, content: dict = {}) -> list:
        '''
        获取一个event_name的所有订阅者 (限小任务) 的返回一个Message列表，注意这个返回结果非标准Message
        '''
        self.registry(event_name)
        content = content or {}
        tasks = [self._ans_executer(fn,**content) for fn in self.handler[event_name]]
        return await asyncio.gather(*tasks, return_exceptions=True)

    async def call(self,request_list:dict = None):
        '''
        一次性启动多个小开销任务，返回一个Message列表，注意这个返回结果非标准Message
        '''
        tasks = []
        name_list = []
        for name,args in request_list.items():
            tasks.append(self._ans_executer(name,**(args or {})))
            name_list.append(name)
        result = await asyncio.gather(*tasks,return_exceptions=True)
        _result = {}
        for res,name in zip(result,name_list):
            _result[name] = res
        
        return _result

    async def _ans_executer(self,func_name:str,**kwargs):
        '''
        执行函数功能的工具，回传Message（异常直接返回 Error 消息，不在外面再包 Done）
        '''
            # 通信采用总线，模块间不再需要回传，所有通信统一使用Message
        async with self.semaphore:
            try:
                result = await self.tools.async_execute(func_name=func_name,**kwargs)
            except Exception as e:
                result = Message(**{'title':'Error','content':{'Task': func_name,'args':kwargs,'error':str(e),'error_type':type(e).__name__}})        # 已是 Message(Error)，下方原样返回
                self._exception_submit({func_name:str(e)})
        return Message(title='Done',content=result)

    async def _executer(self,func_name:str,**kwargs):
        '''执行函数功能的工具，不回传，Tools中的总线回传尚未完成'''
        # 通信采用总线，模块间不再需要回传，所有通信统一使用Message
        async with self.semaphore:
            try:
                await self.tools.async_execute(func_name=func_name,**kwargs)
            except Exception as e:
                self._exception_submit({func_name:str(e)})
        # if handler == None:
        #     handler = func_name                                             #默认使用工具注册时的函数名作为handler
        return None

    async def _run_slow(self, id: int, func_name:str,**kwargs):
        '''慢任务执行器，不回传，执行完毕后将结果写入task_results'''
        try:
            if func_name not in self.reentrant_tasks:
                async with self.semaphore:
                    result = await self.tools.async_execute(func_name=func_name,**kwargs)
            async with self.semaphore:
                result = await self.tools.async_execute(func_name=func_name,**kwargs)
                self.task_results[id] = Message(title='Done',content=result)
        except Exception as e:
            self.task_results[id] = Message(**{'title':'Error','content':{'Task': func_name,'args':kwargs,'error':str(e),'error_type':type(e).__name__}})
            self._exception_submit({func_name:str(e)})
        return None