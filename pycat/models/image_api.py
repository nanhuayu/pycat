"""Provider-owned image wire protocol and operation endpoints; no I/O."""

from dataclasses import dataclass
from urllib.parse import urlsplit

IMAGE_PROTOCOLS = {
    "openai_images": "OpenAI Images（GPT Image）",
    "dashscope_images": "Qwen Image（DashScope 同步）",
    "seedream_images": "Seedream（火山 / BytePlus Ark）",
}
CODEX_IMAGES = "codex_images"
CODEX_IMAGE_BASE = "https://chatgpt.com/backend-api/codex"
_DASHSCOPE_PATH = "/services/aigc/multimodal-generation/generation"


@dataclass(frozen=True)
class ImageAPI:
    protocol: str = "openai_images"
    generation_url: str = ""
    edit_url: str = ""

    def __post_init__(self):
        if self.protocol not in {*IMAGE_PROTOCOLS, CODEX_IMAGES}:
            raise ValueError("未知图像接口类型。")
        for name in ("generation_url", "edit_url"):
            object.__setattr__(self, name, str(getattr(self, name) or "").strip().rstrip("/"))

    @classmethod
    def from_dict(cls, data):
        values = data or {}
        return cls(**{key: values[key] for key in ("protocol", "generation_url", "edit_url") if key in values})

    def to_dict(self):
        return {"protocol": self.protocol, "generation_url": self.generation_url, "edit_url": self.edit_url}

    @classmethod
    def from_provider_data(cls, data):
        if data.get("catalog_key") == "chatgpt" or data.get("auth_type") == "chatgpt":
            return cls(protocol=CODEX_IMAGES)
        if "image_api" in data:
            return cls.from_dict(data["image_api"])
        # Read the previous model-level configuration only at deserialization.
        routes = {
            (model.get("image_protocol") or "openai_images", str(model.get("image_api_base") or "").strip())
            for model in data.get("models", [])
            if isinstance(model, dict)
            and (
                model.get("model_type") == "image"
                or str(model.get("model_id", "")).startswith(("gpt-image-", "chatgpt-image-"))
            )
        }
        if len(routes) > 1:
            raise ValueError(
                f"服务 {data.get('name', '')} 的旧图像模型使用不同接口；请拆分为独立服务后导入，原配置文件未更改。"
            )
        protocol, base = next(iter(routes), ("openai_images", ""))
        return cls(protocol=protocol, generation_url=base, edit_url=base)

    def endpoint(self, default_base: str, *, edit: bool = False) -> str:
        if self.protocol == CODEX_IMAGES:
            return CODEX_IMAGE_BASE + ("/images/edits" if edit else "/images/generations")
        suffix = (
            _DASHSCOPE_PATH
            if self.protocol == "dashscope_images"
            else "/images/edits"
            if edit and self.protocol == "openai_images"
            else "/images/generations"
        )
        address = str((self.edit_url if edit else self.generation_url) or default_base).strip().rstrip("/")
        parsed = urlsplit(address)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("图像地址必须是完整的 HTTP(S) 基础地址或请求地址，不能只填 /v1/images/...。")
        if self.protocol == "dashscope_images" and "/compatible-mode/" in parsed.path:
            raise ValueError("Qwen 原生图像接口请填写以 /api/v1 结尾的地址。")
        if parsed.path.endswith(suffix):
            return address
        known = (
            "/images/generations",
            "/images/edits",
            _DASHSCOPE_PATH,
            "/chat/completions",
            "/responses",
            "/messages",
        )
        if any(parsed.path.endswith(path) for path in known):
            raise ValueError("图像生成或编辑地址的请求路径与所选图像协议不匹配。")
        return address + suffix
