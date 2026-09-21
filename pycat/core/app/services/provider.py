"""
Provider service for managing LLM providers
"""

import re
import threading
from time import monotonic
from typing import List

import httpx

from pycat.core.app.services.codex_auth import CodexAuthService
from pycat.core.app.services.workbuddy_auth import WorkBuddyAuthService
from pycat.models.model_profile import REASONING_MODES, ModelProfile
from pycat.models.model_ref import normalize_provider_name
from pycat.models.provider import Provider

# Codex gates catalog entries on its client version. Keep a tested fallback
# independent of PyCat's release for when GitHub's latest release is unavailable.
CODEX_MODELS_CLIENT_VERSION = '0.155.0'
CODEX_LATEST_RELEASE_URL = 'https://github.com/openai/codex/releases/latest'


class ProviderService:
    """Handles LLM provider operations"""

    def __init__(self, *, transport_factory=None, codex_auth: CodexAuthService | None = None,
                 workbuddy_auth: WorkBuddyAuthService | None = None):
        self.timeout = 60.0
        self._transport_factory = transport_factory
        self.codex_auth = codex_auth
        self.workbuddy_auth = workbuddy_auth
        self._codex_version = CODEX_MODELS_CLIENT_VERSION
        self._codex_version_check_at = 0.0
        self._codex_version_refreshing = False
        self._codex_version_lock = threading.Lock()

    async def request_headers(self, provider: Provider, model_id: str = '') -> dict[str, str]:
        if provider.auth_type in {'chatgpt', 'workbuddy'}:
            auth = self.account_auth(provider.auth_type)
            if auth is None:
                raise RuntimeError(f'当前宿主未配置 {provider.account_label} 登录服务。')
            return await auth.headers(provider, model_id)
        return provider.get_headers(model_id)

    def account_auth(self, auth_type: str):
        return {'chatgpt': self.codex_auth, 'workbuddy': self.workbuddy_auth}.get(auth_type)

    def close(self):
        for auth in (self.codex_auth, self.workbuddy_auth):
            if auth is not None:
                auth.close()

    async def _latest_codex_version(self, client: httpx.AsyncClient) -> str:
        # Background jobs may use different threads/event loops. Only protect
        # the short cache update; never hold the lock during network I/O.
        with self._codex_version_lock:
            if self._codex_version_refreshing or monotonic() < self._codex_version_check_at:
                return self._codex_version
            self._codex_version_refreshing = True
        retry_after = 300.0
        try:
            # GitHub's stable-release permalink needs no REST API quota or
            # response body. Inspect its redirect without following any target.
            response = await client.head(
                CODEX_LATEST_RELEASE_URL,
                headers={'User-Agent': 'PyCat'},
                timeout=3.0,
                follow_redirects=False,
            )
            if not response.is_redirect:
                raise ValueError('Expected a stable-release redirect')
            location = str(response.request.url.join(response.headers.get('location', '')))
            match = re.fullmatch(r'https://github\.com/openai/codex/releases/tag/rust-v(\d+\.\d+\.\d+)', location)
            if match is None:
                raise ValueError('Unexpected Codex release tag')
            version = match.group(1)
            with self._codex_version_lock:
                if tuple(map(int, version.split('.'))) > tuple(map(int, self._codex_version.split('.'))):
                    self._codex_version = version
            retry_after = 3600.0
        except (httpx.HTTPError, ValueError):
            # Rate limits, offline use, and invalid releases must not prevent
            # account access with the last known version (or bundled fallback).
            pass
        finally:
            with self._codex_version_lock:
                self._codex_version_check_at = monotonic() + retry_after
                self._codex_version_refreshing = False
        return self._codex_version

    async def _request_model_catalog(self, provider: Provider) -> httpx.Response:
        headers = await self.request_headers(provider)
        async with httpx.AsyncClient(timeout=self.timeout, transport=self._transport_factory() if self._transport_factory else None) as client:
            version = await self._latest_codex_version(client) if provider.auth_type == 'chatgpt' else None
            return await client.get(
                provider.get_models_endpoint(),
                params={'client_version': version} if version else None,
                headers=headers,
            )

    async def fetch_models(self, provider: Provider) -> List[ModelProfile]:
        """Fetch available models from a provider"""
        if provider.auth_type == 'workbuddy':
            if self.workbuddy_auth is None:
                raise RuntimeError('当前宿主未配置 WorkBuddy 登录服务。')
            return self._workbuddy_profiles(await self.workbuddy_auth.model_catalog(provider))
        response = await self._request_model_catalog(provider)
        response.raise_for_status()
        if provider.auth_type == 'chatgpt':
            return sorted(self._codex_profiles(response), key=lambda item: item.model_id)

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
    def _workbuddy_profiles(data) -> list[ModelProfile]:
        if not isinstance(data, list):
            raise RuntimeError('WorkBuddy 模型目录格式异常，请稍后重试或手工添加模型。')
        profiles = {}
        for item in data:
            if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not item['id'].strip():
                continue
            tags = item.get('tags') if isinstance(item.get('tags'), list) else []
            if set(tags) & {'text-to-image', 'image-to-image', 'text-to-video', 'completion', 'embedding', 'rerank'}:
                continue
            reasoning = item.get('reasoning') if isinstance(item.get('reasoning'), dict) else {}
            options = ['inherit']
            effort = reasoning.get('effort')
            if effort in REASONING_MODES and effort != 'inherit':
                options.append(effort)
            profile = ModelProfile(
                model_id=item['id'].strip(), display_name=str(item.get('name') or item['id']),
                # Conservative total budget: do not add maxOutputTokens to an input limit.
                context_window=ProviderService._positive_int(item.get('maxInputTokens')),
                max_output_tokens=ProviderService._positive_int(item.get('maxOutputTokens')),
                input_modalities=['text', 'image'] if item.get('supportsImages') is True else ['text'],
                supports_tools=item.get('supportsToolCall') is True,
                supports_reasoning=bool(reasoning), reasoning_codec='chat_reasoning' if reasoning else 'none',
                reasoning_options=options, reasoning_default='inherit',
            )
            profiles[profile.model_id] = profile
        if not profiles:
            raise RuntimeError('WorkBuddy 未返回可用对话模型；请检查账号权限，或手工添加账号支持的模型 ID。')
        return sorted(profiles.values(), key=lambda item: item.model_id)

    @staticmethod
    def _codex_profiles(response: httpx.Response) -> list[ModelProfile]:
        invalid_catalog = 'ChatGPT 模型目录格式异常，请更新 PyCat 后重试。'
        try:
            data = response.json()
        except ValueError:
            raise RuntimeError(invalid_catalog) from None
        if not isinstance(data, dict) or not isinstance(data.get('models'), list):
            raise RuntimeError(invalid_catalog)
        models = [
            ProviderService._codex_profile(item)
            for item in data['models']
            if isinstance(item, dict)
            and isinstance(item.get('slug'), str) and item['slug'].strip()
            and item.get('visibility') != 'hide'
        ]
        if not models:
            raise RuntimeError(
                'ChatGPT 未返回可用模型。请更新 PyCat 后重试；'
                '如仍为空，请检查账号的 Codex 访问权限。'
            )
        return models

    @staticmethod
    def _codex_profile(payload: dict) -> ModelProfile:
        options = ['inherit']
        for level in payload.get('supported_reasoning_levels') or []:
            effort = str(level.get('effort') or '') if isinstance(level, dict) else ''
            if effort in REASONING_MODES and effort not in options:
                options.append(effort)
        modalities = payload.get('input_modalities')
        return ModelProfile(
            model_id=str(payload['slug']), display_name=str(payload.get('display_name') or payload['slug']),
            context_window=ProviderService._positive_int(payload.get('context_window')),
            input_modalities=modalities if isinstance(modalities, list) else ['text'],
            supports_tools=True, supports_reasoning=len(options) > 1,
            reasoning_codec='responses_effort' if len(options) > 1 else 'none',
            reasoning_options=options, reasoning_default='inherit',
        )

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
            if provider.auth_type == 'workbuddy':
                if self.workbuddy_auth is None:
                    raise RuntimeError('当前宿主未配置 WorkBuddy 登录服务。')
                await self.workbuddy_auth.check_connection(provider)
                return True, 'WorkBuddy 账号连接成功；模型权限以同步和实际调用结果为准。'
            response = await self._request_model_catalog(provider)
            if response.status_code == 200:
                if provider.auth_type == 'chatgpt':
                    self._codex_profiles(response)
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
        if provider.supports_image_api and (any(model.model_type == 'image' for model in provider.models)
                or provider.image_api.generation_url or provider.image_api.edit_url):
            try:
                provider.image_api.endpoint(provider.api_base)
                provider.image_api.endpoint(provider.api_base, edit=True)
            except ValueError as exc:
                return False, str(exc)
        return True, "Valid"
