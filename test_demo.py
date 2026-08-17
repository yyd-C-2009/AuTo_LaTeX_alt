import asyncio, time, json
from Tools import Tools
from event_bus import Bus
from Agent import run_agent

# 两个工具：一个快、一个慢
def fast_tool(x):
    return f"fast:{x}"

def slow_tool(x):
    time.sleep(3)          # 模拟 3 秒慢任务
    return f"slow:{x}"

async def main():
    tools = Tools()
    tools.add_tool(fast_tool, time_out=5)
    tools.add_tool(slow_tool, time_out=30)

    bus = Bus(tools, max_concurrency=4)
    bus.mark_slow(["slow_tool"])      # 关键：把 slow_tool 标成慢任务

    # 验证 submit 异步：提交慢任务应立刻返回 task_id，不阻塞 3 秒
    t0 = time.time()
    r = await bus.submit("slow_tool", x="A")
    print(f"submit_tool 用时 {time.time()-t0:.2f}s，返回 {r}")  # 应 ≈0s, status=submitted

    await asyncio.sleep(0.5)
    print(f"poll(立即): {str(bus.poll(r.content['id']))}")   # None=处理中

    await asyncio.sleep(3)
    print(f"poll(3秒后):{str(bus.poll(r.content['id']))}")  # slow:A

asyncio.run(main())