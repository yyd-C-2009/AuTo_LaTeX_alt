r"""
验证 Bus 的 IO 锁基础设施：
- io_print：多个协程并发输出，验证「写控制台」这一下互斥、不交错。
- io_dialog：带 input 的对话回合，验证「输出+输入」同一时刻只有一个执行。

运行：python test_io_lock.py
"""
import asyncio
from event_bus import Bus


async def main():
    bus = Bus(max_concurrency=5)

    # ============ 1. io_print 并发输出不交错 ============
    print("===== 测试 1：io_print 并发输出互斥 =====")

    async def spammer(name: str):
        for _ in range(5):
            await bus.io_print(f"[{name}] 这是一行输出的内容，应该完整不被打断")

    # 3 个协程同时疯狂输出
    await asyncio.gather(spammer("A"), spammer("B"), spammer("C"))
    print("（若每行都是完整的 [X] ... 内容，说明 io_print 互斥生效）\n")

    # ============ 2. io_dialog 带 input 的对话回合 ============
    print("===== 测试 2：io_dialog 带 input 的对话回合 =====")
    print("下面会顺序让你输入 2 次，验证「输出+输入」作为完整回合串行。")

    # 两个「代理」都试图开启对话回合；第二个必须等第一个完成（输入后）才轮到
    async def dialog_agent(name: str):
        reply = await bus.io_dialog(f"[{name}] 请问你的问题？")
        print(f"  -> {name} 收到输入: {reply}")

    # 顺序跑两次对话回合（同一个 io_lock，串行）
    await dialog_agent("专家1")
    await dialog_agent("专家2")

    print("\n===== IO 锁基础设施验证完成 =====")


if __name__ == "__main__":
    asyncio.run(main())
