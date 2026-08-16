import inspect
from pydantic import Field, create_model
from pydantic.fields import FieldInfo
from typing import Annotated,get_origin,get_args,Any,Pattern,Callable
from annotated_types import Gt,Le,Ge,Lt,MaxLen,MinLen,MultipleOf
from pydantic_core import PydanticUndefined
from functools import wraps
from enum import Enum
import asyncio

class SPECIAL_ERROR:
    pass

def Field_to_dict(field : FieldInfo):               #当前版本中，应当使用.asdict()返回FieldInfo中的所有字段

    # print(field.json_schema_extra)
    # return json.loads(field.json_schema_extra)
    result = field.asdict()

    # print(f"{result}")

    return result

def clean_dict(in_dict: dict):

    # print("test\n\n")

    if not isinstance(in_dict,dict):
        return in_dict

    result = {}
    # deleted = []

    for k,v in in_dict.items():
        if isinstance(v,dict):
            result[k] = clean_dict(v)
        elif v is not PydanticUndefined:
            result[k] = v

    return result

def get_annotated_types(value) -> str:
    _type = type(value)
    if _type in [Gt,Lt,Le,Ge,Pattern]:
        return _type.__name__.lower()
    if _type is MinLen:
        return 'min_length'
    if _type is MaxLen:
        return 'max_length'
    if _type is MultipleOf:
        return 'multipleOf'
    return None
    raise ValueError("the value inserted is not a annotation type,please check the code.")

def dict_unpack(in_dict: dict):
    result = {}
    for value in in_dict["metadata"]:
        name = get_annotated_types(value)
        if name is None:
            continue
        attr_val = getattr(value,name,SPECIAL_ERROR)                      #
        if attr_val is not SPECIAL_ERROR:
            result[name] = attr_val

    for key,value in in_dict["attributes"].items():
        result[key] = value                                     #注意，annotation字段不应当被解析

    # print(result)
    # result.pop('title')
    return result

def function_to_model(func: Callable):
    result = {}
    for _,inf in inspect.signature(func).parameters.items():

        if get_origin(inf.annotation) is Annotated:

            args = get_args(inf.annotation)
            _description: str = ''
            _field = {}
            _field['attributes'] = {}
            _field['metadata'] = []
            _field["attributes"]['description'] = ''
            for arg in args[1:]:

                if isinstance(arg,str):

                    _description = _description + arg

                elif isinstance(arg,FieldInfo):

                    _field = Field_to_dict(arg)

            if 'attributes' not in _field:
                _field["attributes"] = {}
            if 'metadata' not in _field:
                _field['metadata'] = []

            if _description:

                if "description" not in _field["attributes"] or _field["attributes"]['description'] is None:
                    _field["attributes"]["description"] = _description
                else:
                    _field["attributes"]["description"] = _description + " " + _field["attributes"]["description"]
            
            if inf.default is not inspect._empty:

                _field['attributes']["default"] = inf.default

            _field = clean_dict(_field)
            _field = dict_unpack(_field)
            result[inf.name] = (args[0],Field(**_field))

        else:
            if inf.annotation is inspect._empty:

                base_type = Any

            else:

                base_type = inf.annotation

            if inf.default is not inspect._empty:

                result[inf.name] = (base_type,inf.default)
            else:

                result[inf.name] = (base_type, ...)

    # for k,v in result.items():
    #     print(f"{k} : {v}")

    model = create_model(f"{func.__name__}_model",**result)

    return model
    
class Tools:
    '''工具注册表，工具必须返回dict'''
    def __init__(self):
        self.tool_list = {}         #存储函数列表，便于调用
        self.schema = []            #结构化描述文档，用于向API接口提供信息
        self.timeout = {}
        self.ensure = []            #保证大型工具不会被卸载
        # self.model = []             #存储对应的model，没有实际作用

    def _registry(self,func : Callable):
        schema = {
            "type" : "function",
            "function" : {
                "name" : func.__name__,
                "description" : func.__doc__ if func.__doc__ else func.__name__,
                "parameters" : function_to_model(func=func).model_json_schema()
            }
        }
        
        self.tool_list[func.__name__] = func
        self.schema.append(schema)

        return None

    def add_tool(self,func : Callable,time_out:float = 5.0) -> bool:
        self._registry(func=func)
        self.timeout[func.__name__] = time_out
        return True

    def registry(self,time_out:float = 5.0):

        def decorater(func : function):
        
            @wraps(func)
            def wrapper(*args,**kwargs):
                return func(*args,**kwargs)
            
            self._registry(func)
            self.timeout[func.__name__] = time_out
            
            return wrapper

        return decorater


    def execute(self,func_name:str,*args,**kwargs):
        if func_name in self.tool_list:
            result = None
            try:
                result = self.tool_list[func_name](*args,**kwargs)
            except Exception as e:
                raise RuntimeError(f"工具 {func_name} 抛出异常: {str(e)}")
            return result
        else:
            raise ValueError(f"Function {func_name} not found in tool list.")

    async def async_execute(self,func_name:str,*args,**kwargs):
        if func_name in self.tool_list:
            if inspect.iscoroutinefunction(self.tool_list[func_name]):
                try :
                    return await asyncio.wait_for(
                        self.tool_list[func_name](*args,**kwargs),
                        timeout = self.timeout[func_name]
                    )
                except asyncio.TimeoutError:
                    raise TimeoutError(f"工具 {func_name} 运行超时：time_out = {self.timeout[func_name]}")
                except Exception as e:
                    raise RuntimeError(f"工具 {func_name} 抛出异常: {str(e)}")
            else:
                try:
                    return await asyncio.wait_for(
                        asyncio.to_thread(self.tool_list[func_name],*args,**kwargs),
                        timeout=self.timeout[func_name]
                    )
                except asyncio.TimeoutError:
                    raise TimeoutError(f"工具 {func_name} 运行超时：time_out = {self.timeout[func_name]}")
                except Exception as e:
                    raise RuntimeError(f"工具 {func_name} 抛出异常: {str(e)}")
        else:
            raise ValueError(f"Function {func_name} not found in tool list.")