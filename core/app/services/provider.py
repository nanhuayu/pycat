"""
Provider service for managing LLM providers
"""

from typing import List

import httpx

from models.model_profile import REASONING_MODES, ModelProfile
from models.model_ref import normalize_provider_name
from models.provider import Provider


class ProviderService:
    """Handles LLM provider operations"""

    def __init__(self):
        self.timeout = 60.0

    async def fetch_models(self, provider: Provider) -> List[ModelProfile]:
        """Fetch available models from a provider"""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(
                provider.get_models_endpoint(),
                headers=provider.get_headers()
            )
            response.raise_for_status()
            data = response.json()

            models: list[ModelProfile] = []
            if provider.is_ollama_chat and isinstance(data, dict) and isinstance(data.get("models"), list):
                for model in data["models"]:
                    model_id = model.get("name", "") if isinstance(model, dict) else ""
                    if model_id:
                        models.append(ModelProfile.from_model_id(model_id))
            elif isinstance(data, dict) and isinstance(data.get("data"), list):
                for model in data["data"]:
                    if not isinstance(model, dict):
                        continue
                    profile = (
                        self._openrouter_profile(model)
                        if bool(getattr(provider, "is_openrouter_route", False))
                        else ModelProfile.from_model_id(str(model.get("id") or ""))
                    )
                    if profile.model_id:
                        models.append(profile)

            return sorted(models, key=lambda item: item.model_id)

    @staticmethod
    def _openrouter_profile(payload: dict) -> ModelProfile:
        model_id = str(payload.get("id") or "").strip()
        architecture = payload.get("architecture") if isinstance(payload.get("architecture"), dict) else {}
        input_modalities = architecture.get("input_modalities")
        if not isinstance(input_modalities, list):
            input_modalities = ["text"]
            modality = str(architecture.get("modality") or "").lower()
            if "image" in modality:
                input_modalities.append("image")
        # Audio and output modalities are deliberately not promoted to the
        # runtime profile until their request/response path is closed.
        input_modalities = [
            str(item).strip().lower()
            for item in input_modalities
            if str(item).strip().lower() in {"text", "image"}
        ] or ["text"]

        top_provider = payload.get("top_provider") if isinstance(payload.get("top_provider"), dict) else {}
        raw_parameters = payload.get("supported_parameters")
        raw_parameters = raw_parameters if isinstance(raw_parameters, list) else []

        context_candidates = [
            ProviderService._positive_int(payload.get("context_length")),
            ProviderService._positive_int(top_provider.get("context_length")),
        ]
        context_window = min(value for value in context_candidates if value is not None) if any(
            value is not None for value in context_candidates
        ) else None

        reasoning = payload.get("reasoning") if isinstance(payload.get("reasoning"), dict) else {}
        mandatory = bool(reasoning.get("mandatory", False))
        reasoning_options = ["inherit"]
        if reasoning and not mandatory:
            reasoning_options.append("off")
        if "supported_efforts" in reasoning and reasoning.get("supported_efforts") is None:
            efforts = ("minimal", "low", "medium", "high", "xhigh", "max")
        else:
            raw_efforts = reasoning.get("supported_efforts")
            efforts = raw_efforts if isinstance(raw_efforts, list) else []
        for effort in efforts:
            value = str(effort or "").strip().lower()
            if value == "none":
                value = "off"
            if value in REASONING_MODES and value not in reasoning_options:
                reasoning_options.append(value)
        if mandatory:
            reasoning_options = [item for item in reasoning_options if item != "off"]
        default_effort = str(reasoning.get("default_effort") or "").strip().lower()
        if default_effort == "none":
            default_effort = "off"
        if default_effort in REASONING_MODES and default_effort not in reasoning_options:
            if default_effort != "off" or not mandatory:
                reasoning_options.append(default_effort)
        if reasoning.get("default_enabled") is True and not efforts and not mandatory:
            if "on" not in reasoning_options:
                reasoning_options.append("on")
        if reasoning.get("default_enabled") is False and "off" in reasoning_options:
            reasoning_default = "off"
        elif default_effort in reasoning_options:
            reasoning_default = default_effort
        elif reasoning.get("default_enabled") is True and "on" in reasoning_options:
            reasoning_default = "on"
        else:
            reasoning_default = "inherit"

        return ModelProfile(
            model_id=model_id,
            display_name=str(payload.get("name") or model_id).strip(),
            context_window=context_window,
            max_output_tokens=ProviderService._positive_int(top_provider.get("max_completion_tokens")),
            supports_tools=(
                not raw_parameters or "tools" in raw_parameters or "tool_choice" in raw_parameters
            ),
            supports_reasoning=bool(reasoning),
            input_modalities=input_modalities,
            reasoning_codec="chat_reasoning" if reasoning else "none",
            reasoning_options=reasoning_options,
            reasoning_default=reasoning_default,
            source_url=f"https://openrouter.ai/{model_id}" if model_id else "",
        )

    @staticmethod
    def _positive_int(value) -> int | None:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

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
