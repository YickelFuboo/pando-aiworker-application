import json
import os
from abc import ABC, abstractmethod
from copy import copy
from typing import Dict, Any, Optional, Type, TypeVar, Generic
from app.config.settings import PROJECT_BASE_DIR

T = TypeVar('T')

class BaseModelFactory(ABC, Generic[T]):
    """模型工厂基类，提供通用的模型管理功能"""
    
    @property
    @abstractmethod
    def _models(self) -> Dict[str, Type[T]]:
        """模型类映射字典，子类必须实现"""
        pass

    def __init__(self, config_filename: str):
        """
        初始化工厂类
        
        Args:
            config_filename: 配置文件名称
        """
        self.config_path = os.path.join(PROJECT_BASE_DIR, "app", "config", config_filename)
        self._config = None
        self.load_config()
    
    def load_config(self):
        """加载模型配置文件"""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                self._config = json.load(f)
        except FileNotFoundError:
            raise FileNotFoundError(f"模型配置文件未找到: {self.config_path}")
        except json.JSONDecodeError as e:
            raise ValueError(f"模型配置文件格式错误: {self.config_path} {e}")
    
    def get_supported_models(self) -> Dict[str, Any]:
        """
        获取支持的供应方和模型支持的全集（只返回is_valid为1的）
        
        Returns:
            Dict[str, Any]: 所有支持的模型信息，结构为：
            {
                "provider_name": {
                    "description": "provider_description",
                    "models": {
                        "model_name": {
                            "description": "model_description"
                        }
                    }
                }
            }
        """
        result = {}
        all_models = self._config.get("models", {})
        
        for provider_name, provider_config in all_models.items():
            # 只处理is_valid为1的provider
            if provider_config.get("is_valid", 0) == 1:
                provider_info = {
                    "description": provider_config.get("description", ""),
                    "models": {}
                }
                
                # 获取该provider下的所有模型
                instances = provider_config.get("instances", {})
                for model_name, model_config in instances.items():
                    provider_info["models"][model_name] = {
                        "description": model_config.get("description", "")
                    }
                
                # 只有当该provider有有效模型时才添加到结果中
                if provider_info["models"]:
                    result[provider_name] = provider_info
        
        return result

    def if_model_support(self, provider: str, model: str) -> bool:
        """
        判断模型是否支持
        """
        models = self._config.get("models", {})
        if provider not in models:
            return False

        if models[provider].get("is_valid", 0) != 1:
            return False

        if model not in models[provider].get("instances", {}):
            return False

        return True

    
    def get_model_info_by_name(self, model: str) -> Dict[str, Any]:
        """
        根据模型名称获取模型信息
        编译当前配置文件中所有Provider下的模型，找到名字匹配的模型
        
        Args:
            model: 模型名称
            
        Returns:
            Dict[str, Any]: 包含以下字段的字典：
            {
                "provider": str,        # 供应商名称
                "model_name": str,      # 模型名称
                "provider_info": Dict,  # 供应商信息
                "model_info": Dict      # 模型信息
            }
        """
        models = self._config.get("models", {})
        
        # 遍历所有provider
        for provider_name, provider_config in models.items():
            # 只处理is_valid为1的provider
            if provider_config.get("is_valid", 0) == 1:
                instances = provider_config.get("instances", {})
                
                # 检查该provider下是否有匹配的模型
                if model in instances:
                    return {
                        "provider": provider_name,
                        "model_name": model,
                        "provider_info": {
                            "description": provider_config.get("description", ""),
                            "api_key": provider_config.get("api_key", ""),
                            "base_url": provider_config.get("base_url", ""),
                            "is_valid": provider_config.get("is_valid", 0)
                        },
                        "model_info": instances[model]
                    }
        
        # 如果没有找到匹配的模型
        raise ValueError(f"未找到模型 '{model}'，请检查模型名称是否正确")
        
    def get_default_model(self) -> tuple[str, str]:
        """
        获取默认模型
        
        Returns:
            tuple[str, str]: (供应商名字, 模型名字)
        """
        default_config = self._config.get("default", {})
        
        provider = default_config.get("provider", "")
        model = default_config.get("model", "")

        return provider, model
    
    def get_model_params(self, provider: str, model: str) -> Dict[str, Any]:
        """
        获取模型参数（合并provider和model配置）
        
        Args:
            provider: 供应商名称
            model: 模型名称
            
        Returns:
            Dict[str, Any]: 包含以下字段的字典：
            {
                "success": bool,  # 成功标志
                "api_key": str,   # API密钥
                "base_url": str,  # 基础URL
                "model_params": Dict[str, Any]  # 模型参数集
            }
        """
        try:
            # 直接获取配置
            models = self._config.get("models", {})
            if provider not in models:
                raise ValueError(f"不支持的模型供应商: {provider}")
            
            provider_config = models[provider]
            
            # 检查provider是否有效
            if provider_config.get("is_valid", 0) != 1:
                raise ValueError(f"供应商 {provider} 未启用")
            
            instances = provider_config.get("instances", {})
            if model not in instances:
                raise ValueError(f"供应商 {provider} 不支持模型: {model}")
            
            model_config = instances[model]
            
            return {
                "success": True,
                "api_key": provider_config.get("api_key", ""),
                "base_url": provider_config.get("base_url", ""),
                "model_params": model_config
            }
        except Exception as e:
            return {
                "success": False,
                "api_key": "",
                "base_url": "",
                "model_params": {},
                "error": str(e)
            }
    
    def create_model(self, 
        provider: Optional[str] = None, 
        model: Optional[str] = None, 
        api_key: Optional[str] = None, 
        language: Optional[str] = "Chinese", 
        **kwargs) -> T:
        """
        创建模型实例
        
        Args:
            provider: 供应商名称，如果为None则使用默认值
            model: 模型名称，如果为None则使用默认值
            api_key: API密钥，如果为None则使用配置中的值
            **kwargs: 其他参数
            
        Returns:
            T: 模型实例
        """

        default_provider, default_model = self.get_default_model()

        # 未指定则使用默认模型
        if not model:
            provider, model = default_provider, default_model
        # 只指定了模型名字，根据模型名字找provider
        elif not provider:
            info = self.get_model_info_by_name(model)
            if info:
                provider = info["provider"]
                model = info["model_name"]
            else:
                provider, model = default_provider, default_model
        # 指定的模型无效，也使用默认模型
        elif not self.if_model_support(provider, model):
            provider, model = default_provider, default_model
        
        # 获取模型参数
        model_para = self.get_model_params(provider, model)
        if not model_para["success"]:
            raise ValueError(f"获取模型参数失败: {model_para.get('error', '未知错误')}")

        # 合并模型参数
        config = copy(kwargs)
        for key, value in model_para["model_params"].items():
            if key not in config:
                config[key] = value
        
        # 获取模型类
        model_class = self._models[provider]
        if not model_class:
            raise ValueError(f"未知的模型类: {provider}")
        
        # 创建模型实例
        return model_class(
            api_key = api_key or model_para["api_key"],
            model_name = model,
            base_url = model_para["base_url"],
            language = language,
            **kwargs
        )