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

import asyncio
from Tools import Tools
from event_bus import Bus

async def heartbeat():
    for i in range(5):
        await asyncio.sleep(0.5)
        print(f"心跳 {i}")

def slow_task(tag):
    import time
    time.sleep(2)        # 模拟 2 秒重任务（会走 to_thread，不卡循环）
    print(f"[{tag}] 重任务完成")

def fast_task(tag):
    print(f"[{tag}] 轻任务完成")

async def main():
    tools = Tools()
    tools.add_tool(slow_task, time_out=10)
    tools.add_tool(fast_task, time_out=2)

    bus = Bus(tools)
    bus.subscribe(event_name="ocr.done", func_name="slow_task")
    bus.subscribe(event_name="ocr.done", func_name="fast_task")

    bus.emit("ocr.done", {"tag": "A"})
    bus._end()
    await asyncio.gather(bus.deliver(),heartbeat())

asyncio.run(main())