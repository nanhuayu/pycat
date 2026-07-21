"""
Provider service for managing LLM providers
"""

import httpx
from typing import List, Optional
from models.model_profile import ModelProfile
from models.provider import (
    ANTHROPIC_NATIVE,
    OPENAI_COMPATIBLE,
    OPENAI_RESPONSES,
    OLLAMA_CHAT,
    Provider,
    normalize_provider_name,
)


class ProviderService:
    """Handles LLM provider operations"""
    
    def __init__(self):
        self.timeout = 60.0
    
    async def fetch_models(self, provider: Provider) -> List[str]:
        """Fetch available models from a provider"""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(
                provider.get_models_endpoint(),
                headers=provider.get_headers()
            )
            response.raise_for_status()
            data = response.json()

            models = []
            if provider.is_ollama_chat and isinstance(data, dict) and isinstance(data.get("models"), list):
                for model in data["models"]:
                    model_id = model.get("name", "") if isinstance(model, dict) else ""
                    if model_id:
                        models.append(model_id)
            elif isinstance(data, dict) and isinstance(data.get("data"), list):
                for model in data["data"]:
                    model_id = model.get("id", "") if isinstance(model, dict) else ""
                    if model_id:
                        models.append(model_id)

            return sorted(set(models))
    
    async def test_connection(self, provider: Provider) -> tuple[bool, str]:
        """Test connection to a provider"""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    provider.get_models_endpoint(),
                    headers=provider.get_headers()
                )
                
                if response.status_code == 200:
                    return True, "Connection successful"
                elif response.status_code == 401:
                    return False, "Authentication failed - check API key"
                elif response.status_code == 404:
                    return False, "Endpoint not found - check API base URL"
                else:
                    return False, f"Error: HTTP {response.status_code}"
        except httpx.TimeoutException:
            return False, "Connection timeout"
        except httpx.ConnectError:
            return False, "Could not connect to server"
        except Exception as e:
            return False, f"Error: {str(e)}"
    
    def validate_provider(self, provider: Provider) -> tuple[bool, str]:
        """Validate provider configuration"""
        provider.name = normalize_provider_name(provider.name)
        if not provider.name.strip():
            return False, "Provider name is required"
        if not provider.api_base.strip():
            return False, "API base URL is required"
        if provider.requires_api_key and not provider.api_key.strip():
            return False, "API key is required"
        if not provider.api_base.startswith(('http://', 'https://')):
            return False, "API base URL must start with http:// or https://"
        return True, "Valid"
    
    @staticmethod
    def create_default_providers() -> List[Provider]:
        """Create default provider configurations"""
        return [
            Provider(
                name="openai",
                api_type=OPENAI_COMPATIBLE,
                api_base="https://api.openai.com/v1",
                models=[ModelProfile.from_model_id(model) for model in ("gpt-4o-mini", "gpt-4o", "gpt-4-turbo")],
                supports_vision=True,
                supports_reasoning=False
            ),
            Provider(
                name="openai-responses",
                api_type=OPENAI_RESPONSES,
                api_base="https://api.openai.com/v1",
                models=[
                    ModelProfile.from_model_id(model, supports_reasoning=model.startswith("gpt-5"))
                    for model in ("gpt-5-mini", "gpt-5.1", "gpt-4.1", "gpt-4.1-mini")
                ],
                supports_vision=True,
                supports_reasoning=True,
            ),
            Provider(
                name="anthropic",
                api_type=ANTHROPIC_NATIVE,
                api_base="https://api.anthropic.com/v1",
                models=[
                    ModelProfile.from_model_id(model, supports_reasoning=True)
                    for model in ("claude-3-5-sonnet-20241022", "claude-3-opus-20240229", "claude-3-haiku-20240307")
                ],
                supports_vision=True,
                supports_reasoning=True,
                custom_headers={"anthropic-version": "2023-06-01"}
            ),
            Provider(
                name="ollama",
                api_type=OLLAMA_CHAT,
                api_base="http://localhost:11434",
                models=[],
                supports_vision=True,
                supports_reasoning=False
            )
        ]
