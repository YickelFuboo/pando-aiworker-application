from typing import Dict, Type
from app.infrastructure.llms.base_factory import BaseModelFactory
from app.infrastructure.llms.chat_models.base.base import LLM
from app.infrastructure.llms.chat_models.deepseek_llm import DeepSeekModels
from app.infrastructure.llms.chat_models.claude_llm import ClaudeModels
from app.infrastructure.llms.chat_models.openai_llm import OpenAIModels
from app.infrastructure.llms.chat_models.qwen_llm import QwenModels
from app.infrastructure.llms.chat_models.siliconflow_llm import SiliconFlowModels
from app.infrastructure.llms.chat_models.fuyao_llm import FuYaoModels
from app.infrastructure.llms.chat_models.zhipu_llm import ZhiPuModels

# =============================================================================
# 聊天模型工厂
# =============================================================================

class LLMFactory(BaseModelFactory):
    """聊天模型工厂类"""
    
    @property
    def _models(self) -> Dict[str, Type[LLM]]:
        return {
            "deepseek": DeepSeekModels,
            "claude": ClaudeModels,
            "openai": OpenAIModels,
            "qwen": QwenModels,
            "siliconflow": SiliconFlowModels,
            "fuyao": FuYaoModels,
            "zhipu": ZhiPuModels,
        }
    
    def __init__(self):
        super().__init__("chat_models.json")
    

# 全局工厂实例
llm_factory = LLMFactory()