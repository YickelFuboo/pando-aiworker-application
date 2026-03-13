import asyncio
import json
from typing import Dict, Optional, List, Literal, Union, AsyncGenerator, Any, Tuple
import logging
from anthropic import AsyncAnthropic
from app.infrastructure.llms.chat_models.base.openai_base import OpenAIBase
from app.infrastructure.llms.chat_models.base.base import MAX_RETRY_ATTEMPTS
from app.infrastructure.llms.chat_models.schemes import ChatResponse, AskToolResponse, ToolInfo


class ClaudeModels(OpenAIBase):
    """Anthropic Claude模型系列"""
    
    def __init__(self, api_key: str, model_name: str = "claude-3-5-sonnet-20241022", base_url: str = "https://api.anthropic.com", language: str = "Chinese", **kwargs):
        """
        初始化Claude模型
        
        Args:
            api_key (str): Anthropic API密钥
            model_name (str): 模型名称，默认为claude-3-5-sonnet-20241022
            base_url (str): API基础URL，默认为Anthropic官方API
            language (str): 语言设置
            **kwargs: 其他参数
        """
        super().__init__(api_key, model_name, base_url, language, **kwargs)
        
        # 创建Claude客户端
        self.client = AsyncAnthropic(
            api_key=api_key,
            base_url=base_url,
            timeout=60.0
        )

    def _format_message(
        self,
        system_prompt: str, 
        user_prompt: str, 
        user_question: str,
        history: Optional[List[Dict[str, Any]]] = None
    ) -> List[Dict[str, Any]]:
        """格式化消息为 Claude API 所需的格式（将system prompt合并到用户消息中）"""
        try:
            messages = []
            
            # 添加历史消息
            if history:
                messages.extend(history)
 
            # 如果有单独的用户问题信息，则添加用户消息（包含system prompt）
            if user_question or system_prompt:
                user_message = f"{user_prompt}\n{user_question}" if user_prompt else user_question
                if system_prompt:
                    user_message = f"{system_prompt}\n\n{user_message}"
                messages.append({"role": "user", "content": user_message})
        
            if not messages:
                logging.error("Messages are empty")
                raise ValueError("Messages are empty")
            
            return messages
        except Exception as e:
            logging.error(f"Error in _format_message: {e}")
            raise e

    async def chat(self, 
                  system_prompt: str,
                  user_prompt: str,
                  user_question: str,
                  history: List[Dict[str, Any]] = None,
                  with_think: Optional[bool] = False,
                  **kwargs) -> Tuple[ChatResponse, int]:
        """Claude风格的聊天实现，支持失败重试"""
        messages = self._format_message(
            system_prompt, user_prompt, user_question, history
        )

        # 构建参数
        params = {
            "model": self.model_name,
            "messages": messages
        }
        # 添加其他参数，避免重复
        for key, value in kwargs.items():
            if key not in params:
                params[key] = value
        
        # 实现重试策略
        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                response = await self.client.messages.create(**params)
                
                # 检查响应结构是否有效
                if not response.content or len(response.content) == 0:
                    return ChatResponse(
                        content="Invalid response structure",
                        success=False
                    ), 0
                
                # 获取回答内容
                content = response.content[0].text.strip()
                
                # 检查是否因长度限制截断
                if response.stop_reason == "max_tokens":
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
        """Claude风格的流式聊天实现，支持失败重试"""
        messages = self._format_message(
            system_prompt, user_prompt, user_question, history
        )

        # 构建参数
        params = {
            "model": self.model_name,
            "messages": messages,
            "stream": True
        }
        # 添加其他参数，避免重复
        for key, value in kwargs.items():
            if key not in params:
                params[key] = value

        # 实现重试策略
        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                response = await self.client.messages.create(**params)
                
                # 检查响应结构是否有效
                if not response:
                    return self._create_error_stream("Invalid response structure"), 0
                
                total_tokens = 0
                
                async def stream_response():
                    nonlocal total_tokens
                    
                    try:
                        async for chunk in response:
                            content = ""
                            
                            if chunk.type == "content_block_delta":
                                if hasattr(chunk.delta, 'text'):
                                    content = chunk.delta.text
                            
                            # 统计tokens（Claude流式响应中可能不包含usage信息）
                            if hasattr(chunk, 'usage') and chunk.usage:
                                total_tokens = self._total_token_count(chunk)

                            # 如果超长截断，则添加截断提示
                            if hasattr(chunk, 'stop_reason') and chunk.stop_reason == "max_tokens":
                                content = self._add_truncate_notify(content)

                            if content:
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
        """Claude风格的工具调用实现，支持失败重试"""
        if tool_choice == "required" and not tools:
            return AskToolResponse(
                content="tool_choice 为 'required' 时必须提供 tools",
                success=False
            ), 0
        
        messages = self._format_message(
            system_prompt, user_prompt, user_question, history
        )
        
        params = {
            "model": self.model_name,
            "messages": messages
        }

        if tools and tool_choice != "none":
            # 转换工具格式为Claude格式
            claude_tools = []
            for tool in tools:
                claude_tool = {
                    "name": tool["function"]["name"],
                    "description": tool["function"].get("description", ""),
                    "input_schema": tool["function"]["parameters"]
                }
                claude_tools.append(claude_tool)
            params["tools"] = claude_tools
            
            if tool_choice == "required":
                params["tool_choice"] = {"type": "any"}
            elif tool_choice == "auto":
                params["tool_choice"] = {"type": "auto"}
        
        # 添加其他参数，避免重复
        for key, value in kwargs.items():
            if key not in params:
                params[key] = value

        # 实现重试策略
        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                response = await self.client.messages.create(**params)
                
                # 检查响应结构是否有效
                if not response.content:
                    return AskToolResponse(
                        content="Invalid response structure",
                        success=False
                    ), 0
                
                # 处理响应
                content = ""
                tool_calls = []
                
                for content_block in response.content:
                    if content_block.type == "text":
                        content += content_block.text
                    elif content_block.type == "tool_use":
                        tool_calls.append(ToolInfo(
                            id=content_block.id,
                            name=content_block.name,
                            args=content_block.input
                        ))
                
                return AskToolResponse(
                    content=content,
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
        """Claude风格的工具调用流式实现，支持失败重试"""
        if tool_choice == "required" and not tools:
            return self._create_error_stream("tool_choice 为 'required' 时必须提供 tools"), 0
        
        messages = self._format_message(
            system_prompt, user_prompt, user_question, history
        )
        
        params = {
            "model": self.model_name,
            "messages": messages,
            "stream": True
        }

        if tools and tool_choice != "none":
            # 转换工具格式为Claude格式
            claude_tools = []
            for tool in tools:
                claude_tool = {
                    "name": tool["function"]["name"],
                    "description": tool["function"].get("description", ""),
                    "input_schema": tool["function"]["parameters"]
                }
                claude_tools.append(claude_tool)
            params["tools"] = claude_tools
            
            if tool_choice == "required":
                params["tool_choice"] = {"type": "any"}
            elif tool_choice == "auto":
                params["tool_choice"] = {"type": "auto"}
        
        # 添加其他参数，避免重复
        for key, value in kwargs.items():
            if key not in params:
                params[key] = value

        # 实现重试策略
        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                response = await self.client.messages.create(**params)
                
                # 检查响应结构是否有效
                if not response:
                    return self._create_error_stream("Invalid response structure"), 0
                
                total_tokens = 0
                
                async def stream_response():
                    nonlocal total_tokens
                    tool_calls_collected = {}
                    
                    try:
                        async for chunk in response:
                            content = ""
                            
                            if chunk.type == "content_block_delta":
                                if hasattr(chunk.delta, 'text'):
                                    content = chunk.delta.text
                            elif chunk.type == "tool_use_block_start":
                                # 开始工具调用
                                tool_id = chunk.tool_use.id
                                tool_calls_collected[tool_id] = {
                                    "id": tool_id,
                                    "name": chunk.tool_use.name,
                                    "arguments": ""
                                }
                            elif chunk.type == "tool_use_block_delta":
                                # 累积工具参数
                                if chunk.delta and chunk.delta.partial_json:
                                    tool_id = chunk.tool_use_id
                                    if tool_id in tool_calls_collected:
                                        tool_calls_collected[tool_id]["arguments"] += chunk.delta.partial_json
                            
                            # 统计tokens
                            if hasattr(chunk, 'usage') and chunk.usage:
                                total_tokens = self._total_token_count(chunk)

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