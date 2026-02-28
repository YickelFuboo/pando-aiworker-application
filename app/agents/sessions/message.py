import json
from enum import Enum
from pydantic import BaseModel, Field, model_validator
from typing import Optional, List, Dict, Any, Union
from datetime import datetime


class Role(str, Enum):
    """消息角色"""
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"

class Function(BaseModel):
    name: str
    arguments: str

    def model_dump(self) -> Dict[str, Any]:
        """自定义序列化方法"""
        arguments = self.arguments
        if isinstance(arguments, (dict, list)):
            arguments = json.dumps(arguments)
        elif isinstance(arguments, str):
            # 如果已经是字符串，尝试解析并重新序列化以确保格式正确
            try:
                arguments = json.dumps(json.loads(arguments), ensure_ascii=False)
            except json.JSONDecodeError:
                # 如果不是有效的 JSON 字符串，直接序列化
                arguments = json.dumps(arguments, ensure_ascii=False)
        return {
            "name": self.name,
            "arguments": arguments
        }

class ToolCall(BaseModel):
    """助手发起的单次工具调用（assistant 消息中）。"""
    id: str
    type: str = "function"
    function: Function

    def model_dump(self) -> Dict[str, Any]:
        """自定义序列化方法"""
        return {
            "id": self.id,
            "type": self.type,
            "function": self.function.model_dump()
        }

class Message(BaseModel):
    """聊天消息，兼容 OpenAI 风格。按用途分三种形态：

    - 普通消息：role + content（system/user/assistant 的普通回复）
    - 助手工具调用：role="assistant" + tool_calls（可选 content）
    - 工具执行结果：role="tool" + tool_result + content
    """
    role: Role
    content: str = ""

    # 模型返回的工具调用信息记录
    tool_calls: Optional[List[ToolCall]] = Field(default=None, description="助手发起的工具调用列表，仅 role=assistant 时使用")

    # 工具执行结果信息
    name: Optional[str] = Field(default=None, description="工具名")
    tool_call_id: Optional[str] = Field(default=None, description="对应 assistant 消息里 tool_calls[].id")

    create_time: Optional[datetime] = Field(default=None)

    @property
    def is_tool_result(self) -> bool:
        """是否为工具执行结果消息（role=tool 且带 tool_result）。"""
        return self.role == Role.TOOL and self.name is not None and self.tool_call_id is not None

    @property
    def is_assistant_tool_calls(self) -> bool:
        """是否为助手工具调用消息（role=assistant 且带 tool_calls）。"""
        return self.role == Role.ASSISTANT and bool(self.tool_calls)

    def model_dump(self) -> Dict[str, Any]:
        """自定义序列化方法，对外仍输出 name/tool_call_id 以兼容 API 与存储。"""
        message = {"role": self.role.value}
        if self.content is not None:
            message["content"] = self.content
        if self.tool_calls is not None:
            message["tool_calls"] = [tool_call.model_dump() for tool_call in self.tool_calls]
        if self.name is not None and self.tool_call_id is not None:
            message["name"] = self.name
            message["tool_call_id"] = self.tool_call_id
        if self.create_time:
            message["create_time"] = self.create_time.strftime("%Y-%m-%d %H:%M:%S")
        return message

    def to_context(self) -> Dict[str, Any]:
        """提供给 LLM API 的消息格式，不包含 create_time。"""
        message = {"role": self.role.value}
        if self.content is not None:
            message["content"] = self.content
        if self.tool_calls is not None:
            message["tool_calls"] = [tc.model_dump() for tc in self.tool_calls]
        if self.name is not None and self.tool_call_id is not None:
            message["name"] = self.name
            message["tool_call_id"] = self.tool_call_id
        return message

    def to_json(self) -> str:
        """将消息转换为JSON字符串"""
        return json.dumps(self.model_dump(), ensure_ascii=False)

    def to_user_message(self) -> Dict[str, Any]:
        """将消息转换易于用户阅读的消息格式"""
        message = {'role': self.role.value}

        # 添加内容
        content = ""
        if self.tool_calls:
            if self.content:
                content = self.content
                content += "\n\n"
            # 添加toolcall信息，格式：工具执行：web_search("北京天气")，other_tool("arg")
            content += "工具执行："
            parts = []
            for tool_call in self.tool_calls:
                args_dict = json.loads(tool_call.function.arguments or "{}")
                args_str = ", ".join(
                    f'"{v}"' if isinstance(v, str) else str(v)
                    for v in args_dict.values()
                )
                parts.append(f"{tool_call.function.name}({args_str})")
            content += "，".join(parts)
        elif self.name and self.tool_call_id:
            content = f"  工具{self.name}执行结果：\n\n{self.content}"
        else:
            content = self.content
        message['content'] = content

        # 添加创建时间
        message['create_time'] = self.create_time.strftime("%Y-%m-%d %H:%M:%S")
        
        return message

    @classmethod
    def system_message(cls, content: str) -> "Message":
        """创建系统消息"""
        return cls(role="system", content=content)

    @classmethod
    def user_message(cls, content: str) -> "Message":
        """创建用户消息"""
        return cls(role="user", content=content, create_time=datetime.now())

    @classmethod
    def assistant_message(cls, content: Optional[str] = None) -> "Message":
        """创建助手消息"""
        return cls(role="assistant", content=content, create_time=datetime.now())

    @classmethod
    def tool_call_message(cls, content: Union[str, List[str]] = "", tool_calls: Optional[List[ToolCall]] = None, **kwargs) -> "Message":
        """创建带工具调用的助手消息。"""
        formatted_calls = [
            {"id": call.id, "type": call.type, "function": call.function.model_dump()}
            for call in (tool_calls or [])
        ]
        return cls(
            role="assistant", content=content, tool_calls=formatted_calls, create_time=datetime.now(), **kwargs
        )

    @classmethod
    def tool_result_message(cls, content: str, name: str, tool_call_id: str) -> "Message":
        """创建工具执行结果消息（role=tool）。"""
        return cls(
            role="tool",
            content=content,
            name=name,
            tool_call_id=tool_call_id,
            create_time=datetime.now(),
        )
