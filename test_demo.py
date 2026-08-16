# from Tools import Tools
# from event_bus import Bus
# import asyncio

# def on_order(content):
#     print("收到订单事件:", content)

# def on_order_bad(content):
#     raise RuntimeError("我是坏订阅者")

# tools = Tools()
# tools.add_tool(on_order)
# tools.add_tool(on_order_bad)

# bus = Bus(tools)
# bus.subscribe(event_name="order.created", func_name="on_order")
# bus.subscribe(event_name="order.created",func_name= "on_order_bad")

# bus.emit("order.created", {'content':"订单#1001"})
# bus._end()
# asyncio.run(bus.deliver())

import time
import asyncio
from Tools import Tools
from event_bus import Bus


# —— 15 个重任务订阅者，每个 sleep 0.5 秒 ——
for _i in range(15):
    def worker(tag, idx=_i):                    # 用默认参数 idx 捕获 _i，避免闭包共用
        time.sleep(0.5)
    worker.__name__ = f"worker_{_i}"            # 每个函数名唯一，Tools 注册用
    globals()[worker.__name__] = worker         # 挂到全局，方便后续引用名字

def large_task(tag):
    print('large')
    time.sleep(10)

async def main():
    tools = Tools()
    for i in range(15):
        tools.add_tool(globals()[f"worker_{i}"], time_out=5)

    tools.add_tool(large_task,5)

    # —— 关键：设并发上限为 5 ——
    bus = Bus(tools, max_concurrency=16)

    for i in range(15):
        bus.subscribe(f"worker_{i}", "batch.task")

    bus.subscribe("large_task","batch.task")

    start = time.time()
    bus.emit("batch.task", {"tag": "x"})
    bus._end()
    await bus.deliver()
    elapsed = time.time() - start

    print(f"\n===== max_concurrency=16 总耗时 {elapsed:.2f} 秒 =====")

ss = time.time()
asyncio.run(main())
ed = time.time()
print(f'total:{ed-ss}')