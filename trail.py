import time,os
from pydantic import BaseModel, Field, create_model
from typing import Optional,Annotated,get_origin,Any,Pattern
from enum import Enum
from Tools import Tools
from Saver import Saver
import asyncio

queue = []

async def reader():
    while True:
        if queue:
            print(queue[0])
            queue.pop(0)
    return None

async def main():
    asyncio.create_task(asyncio.to_thread(reader))
    while True:
        text = await asyncio.to_thread(input)
        queue.append(text)

asyncio.run(main())
    