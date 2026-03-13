import asyncio
import json
import time
import logging
from typing import Dict, Optional, List, Literal, Union, AsyncGenerator, Any, Tuple
from openai import AsyncOpenAI
from openai import RateLimitError, APITimeoutError, APIConnectionError, InternalServerError
from app.infrastructure.llms.chat_models.base.base import LLM, MAX_RETRY_ATTEMPTS
from app.infrastructure.llms.chat_models.schemes import ChatResponse, AskToolResponse, ToolInfo
from app.infrastructure.llms.utils import num_tokens_from_string


class OpenAIBase(LLM):
    """OpenAI兼容API的通用实现（适用于OpenAI、DeepSeek、Qwen等）"""
    def __init__(self, api_key: str, model_name: str, base_url: Optional[str] = None, language: str = "Chinese", **kwargs):
        """初始化OpenAI兼容的聊天模型"""
        
        super().__init__(api_key, model_name, base_url, language, **kwargs)

    def _format_message(
        self,
        system_prompt: str, 
        user_prompt: str, 
        user_question: str,
        history: Optional[List[Dict[str, Any]]] = None
    ) -> List[Dict[str, Any]]:
        """格式化消息为 OpenAI API 所需的格式"""
        try:
            messages = []

            # 添加系统提示信息
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            
            # 添加对话历史
            if history:
                messages.extend(history)
 
            # 如果有单独的用户问题信息，则添加用户问题信息
            if user_question:
                user_message = f"{user_prompt}\n{user_question}" if user_prompt else user_question
                messages.append({"role": "user", "content": user_message})

            # 如果messages为空
            if not messages:
                logging.error("Messages are empty")
                raise ValueError("Messages are empty")
        
            return messages
        except Exception as e:
            logging.error(f"Error in _format_openai_message: {e}")
            raise e
    
    async def chat(self, 
                  system_prompt: str,
                  user_prompt: str,
                  user_question: str,
                  history: List[Dict[str, Any]] = None,
                  with_think: Optional[bool] = False,
                  **kwargs) -> Tuple[ChatResponse, int]:
        """OpenAI兼容的聊天实现，支持失败重试"""
        messages = self._format_message(
            system_prompt, user_prompt, user_question, history
        )

        # 构建参数
        params = {"stream": False}
        # 添加其他参数，避免重复
        for key, value in kwargs.items():
            if key not in params:
                params[key] = value
        
        # 实现重试策略
        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                response = await self.client.chat.completions.create(model=self.model_name, messages=messages, **params)
                
                # 检查响应结构是否有效
                if (not response.choices or not response.choices[0].message or  not response.choices[0].message.content):
                    return ChatResponse(
                        content="Invalid response structure",
                        success=False
                    ), 0
                
                # 获取回答内容
                # ps：非流式场景下，即便开启了reasoning_mode: "deep"，也不会返回reasoning_content字段，所有内容
                # （思考 + 答案）合并到Content中返回     
                content = response.choices[0].message.content.strip()
                
                # 检查是否因长度限制截断
                if response.choices[0].finish_reason == "length":
                    content = self._add_truncate_notify(content)

                return ChatResponse(
                    content=content,
                    success=True
                ), self._total_token_count(response)
            
            except Exception as e:
                # 检查是否需要重试
                if not self._is_retryable_error(e) or attempt == MAX_RETRY_ATTEMPTS - 1:
                    logging.error(f"Error in chat (attempt {attempt + 1}): {e}")
                    return ChatResponse(
                        content=str(e),
                        success=False
                    ), 0
                
                # 重试延迟（指数退避）
                delay = self._get_delay(attempt)
                logging.warning(f"Retryable error in chat (attempt {attempt + 1}/{MAX_RETRY_ATTEMPTS}): {e}. Retrying in {delay:.2f}s...")
                await asyncio.sleep(delay)
        
        return ChatResponse(
            content="Unexpected error: max retries exceeded",
            success=False
        ), 0

    
    async def chat_stream(self, 
                  system_prompt: str,
                  user_prompt: str,
                  user_question: str,
                  history: List[Dict[str, Any]] = None,
                  with_think: Optional[bool] = False,
                  **kwargs) -> Tuple[AsyncGenerator[str, None], int]:
        """OpenAI兼容的聊天流式实现，支持失败重试"""
        messages = self._format_message(
            system_prompt, user_prompt, user_question, history
        )

        # 构建参数
        params = {"stream": True}
        # 添加其他参数，避免重复
        for key, value in kwargs.items():
            if key not in params:
                params[key] = value

        # 实现重试策略
        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                # 调用模型接口
                response = await self.client.chat.completions.create(model=self.model_name, messages=messages, **params)
                
                # 检查响应结构是否有效
                if not response:
                    return self._create_error_stream("Invalid response structure"), 0
                
                total_tokens = 0
                
                async def stream_response():
                    nonlocal total_tokens
                    reasoning_start = False  
                    
                    try:
                        async for chunk in response:
                            content = ""

                            # 获取内容
                            if not chunk.choices:
                                continue
                            
                            # 拼接think部分，开启"reasoning_mode": "deep"后有本内容
                            if hasattr(chunk.choices[0].delta, "reasoning_content") and chunk.choices[0].delta.reasoning_content is not None:
                                if not reasoning_start:
                                    reasoning_start = True
                                    content = "<think>"
                                content += chunk.choices[0].delta.reasoning_content
                            
                            # 正式内容拼接
                            if chunk.choices[0].delta.content:
                                if reasoning_start:
                                    content += "</think>"
                                    reasoning_start = False
                                content += chunk.choices[0].delta.content 

                            # 统计tokens
                            tokens = self._total_token_count(chunk)
                            if not tokens:
                                total_tokens += num_tokens_from_string(content)
                            else:
                                total_tokens += tokens

                            # 如果超长截断，则添加截断提示
                            if chunk.choices[0].finish_reason == "length":
                                content = self._add_truncate_notify(content)

                            yield content

                    except Exception as e:
                        logging.error(f"Error in stream response: {e}")
                        if hasattr(response, 'close'):
                            await response.close()
                        raise
                
                # 返回流式响应和token数量
                return stream_response(), total_tokens

            except Exception as e:
                # 检查是否需要重试
                if not self._is_retryable_error(e) or attempt == MAX_RETRY_ATTEMPTS - 1:
                    logging.error(f"Error in chat_stream (attempt {attempt + 1}): {e}")
                    return self._create_error_stream(str(e)), 0
                
                # 重试延迟（指数退避）
                delay = self._get_delay(attempt)
                logging.warning(f"Retryable error in chat_stream (attempt {attempt + 1}/{MAX_RETRY_ATTEMPTS}): {e}. Retrying in {delay:.2f}s...")
                await asyncio.sleep(delay)
        
        return self._create_error_stream("Unexpected error: max retries exceeded"), 0


    async def ask_tools(self,
                       system_prompt: str,
                       user_prompt: str,
                       user_question: str,
                       history: List[Dict[str, Any]] = None,
                       tools: Optional[List[dict]] = None,
                       tool_choice: Literal["none", "auto", "required"] = "auto",
                       with_think: Optional[bool] = False,
                       **kwargs) -> Tuple[AskToolResponse, int]:
        """OpenAI兼容的工具调用实现，支持失败重试"""
        if tool_choice == "required" and not tools:
            return AskToolResponse(
                content="tool_choice 为 'required' 时必须提供 tools",
                success=False
            ), 0
        
        messages = self._format_message(
            system_prompt, user_prompt, user_question, history
        )
        
        params = {"stream": False}
        if tools and tool_choice != "none":
            params["tools"] = tools
            params["tool_choice"] = tool_choice
        
        # 添加其他参数，避免重复
        for key, value in kwargs.items():
            if key not in params:
                params[key] = value

        # 实现重试策略
        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                response = await self.client.chat.completions.create(model=self.model_name, messages=messages, **params)
                
                # 检查响应结构是否有效
                if (not response.choices or not response.choices[0].message):
                    return AskToolResponse(
                        content="Invalid response structure",
                        success=False
                    ), 0
                
                msg = response.choices[0].message
                tool_calls = []
                if hasattr(msg, 'tool_calls') and msg.tool_calls:
                    for tool_call in msg.tool_calls:
                        arguments = tool_call.function.arguments
                        try:
                            args = json.loads(arguments)
                        except json.JSONDecodeError:
                            args = arguments
                        
                        tool_calls.append(ToolInfo(
                            id=tool_call.id,
                            name=tool_call.function.name,
                            args=args
                        ))
                
                return AskToolResponse(
                    content=msg.content or "",
                    tool_calls=tool_calls,
                    success=True
                ), self._total_token_count(response)

            except Exception as e:
                # 检查是否需要重试
                if not self._is_retryable_error(e) or attempt == MAX_RETRY_ATTEMPTS - 1:
                    logging.error(f"Error in ask_tools (attempt {attempt + 1}): {e}")
                    return AskToolResponse(
                        content=str(e),
                        success=False
                    ), 0
                
                # 重试延迟（指数退避）
                delay = self._get_delay(attempt)
                logging.warning(f"Retryable error in ask_tools (attempt {attempt + 1}/{MAX_RETRY_ATTEMPTS}): {e}. Retrying in {delay:.2f}s...")
                await asyncio.sleep(delay)
        
        return AskToolResponse(
            content="Unexpected error: max retries exceeded",
            success=False
        ), 0


    async def ask_tools_stream(self,
                       system_prompt: str,
                       user_prompt: str,
                       user_question: str,
                       history: List[Dict[str, Any]] = None,
                       tools: Optional[List[dict]] = None,
                       tool_choice: Literal["none", "auto", "required"] = "auto",
                       with_think: Optional[bool] = False,
                       **kwargs) -> Tuple[AsyncGenerator[str, None], int]:
        """OpenAI兼容的工具调用流式实现，支持失败重试"""
        if tool_choice == "required" and not tools:
            return self._create_error_stream("tool_choice 为 'required' 时必须提供 tools"), 0
        
        messages = self._format_message(
            system_prompt, user_prompt, user_question, history
        )
        
        params = {"stream": True}
        if tools and tool_choice != "none":
            params["tools"] = tools
            params["tool_choice"] = tool_choice
        
        # 添加其他参数，避免重复
        for key, value in kwargs.items():
            if key not in params:
                params[key] = value

        # 实现重试策略
        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                response = await self.client.chat.completions.create(model=self.model_name, messages=messages, **params)
                
                # 检查响应结构是否有效
                if not response:
                    return self._create_error_stream("Invalid response structure"), 0
                
                total_tokens = 0
                
                async def stream_response():
                    nonlocal total_tokens
                    reasoning_start = False
                    tool_calls_collected = {}  
                    
                    try:
                        async for chunk in response:
                            content = ""
                   
                            if not chunk.choices:
                                continue
                            
                            # 统计tokens
                            tokens = self._total_token_count(chunk)
                            if tokens:
                                total_tokens += tokens

                            # 拼接think部分，开启"reasoning_mode": "deep"后有本内容
                            if hasattr(chunk.choices[0].delta, "reasoning_content") and chunk.choices[0].delta.reasoning_content is not None:
                                if not reasoning_start:
                                    reasoning_start = True
                                    content = "<think>"
                                content += chunk.choices[0].delta.reasoning_content

                            # 正式内容拼接
                            if chunk.choices[0].delta.content:
                                if reasoning_start:
                                    content += "</think>"
                                    reasoning_start = False
                                content += chunk.choices[0].delta.content
                            
                            # 处理工具调用
                            if chunk.choices[0].delta.tool_calls:
                                tool_call = chunk.choices[0].delta.tool_calls[0]
                                if tool_call.function and tool_call.id:
                                    tool_id = tool_call.id
                                    
                                    # 初始化工具调用信息
                                    if tool_id not in tool_calls_collected:
                                        tool_calls_collected[tool_id] = {
                                            "id": tool_id,
                                            "name": tool_call.function.name or "",
                                            "arguments": ""
                                        }
                                    
                                    # 累积参数（流式传递可能是分片的）
                                    # 注意：tool_call.function.arguments 可能为 None
                                    if tool_call.function.arguments is not None:
                                        tool_calls_collected[tool_id]["arguments"] += tool_call.function.arguments
                            
                            
                            # 如果有内容则yield（实时返回）
                            if content:
                                yield content

                        # 处理收集到的工具调用，格式化为字符串
                        if tool_calls_collected:
                            tool_calls_str = self._format_tool_calls(tool_calls_collected)
                            yield tool_calls_str
                    
                    except Exception as e:
                        logging.error(f"Error in stream response: {e}")
                        if hasattr(response, 'close'):
                            await response.close()
                        raise
                
                # 返回流式响应和token数量
                return stream_response(), total_tokens

            except Exception as e:
                # 检查是否需要重试
                if not self._is_retryable_error(e) or attempt == MAX_RETRY_ATTEMPTS - 1:
                    logging.error(f"Error in ask_tools_stream (attempt {attempt + 1}): {e}")
                    return self._create_error_stream(str(e)), 0
                
                # 重试延迟（指数退避）
                delay = self._get_delay(attempt)
                logging.warning(f"Retryable error in ask_tools_stream (attempt {attempt + 1}/{MAX_RETRY_ATTEMPTS}): {e}. Retrying in {delay:.2f}s...")
                await asyncio.sleep(delay)
        
        return self._create_error_stream("Unexpected error: max retries exceeded"), 0
