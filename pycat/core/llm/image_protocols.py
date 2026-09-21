"""Image wire formats, independent of chat envelopes and HTTP lifecycle."""

from __future__ import annotations

from typing import Any

from pycat.core.content.images import RasterImage
from pycat.models.contracts.capability import ImageGenerationOptions
from pycat.models.image_api import CODEX_IMAGES
from pycat.models.model_profile import ModelProfile


def image_request(
    profile: ModelProfile,
    prompt: str,
    options: ImageGenerationOptions,
    images: list[RasterImage],
    mask: RasterImage | None,
    *,
    protocol: str,
) -> dict[str, Any]:
    extra = dict(profile.extra_body)
    reserved = {
        "model",
        "prompt",
        "image",
        "images",
        "mask",
        "input",
        "stream",
        "n",
        "size",
        "quality",
        "output_format",
        "response_format",
        "background",
        "sequential_image_generation",
        "sequential_image_generation_options",
    }
    if reserved.intersection(extra):
        raise ValueError(
            "图像模型附加字段不能覆盖输入、协议或图像参数：" + ", ".join(sorted(reserved.intersection(extra)))
        )
    body = {"model": profile.model_id, "prompt": prompt, **extra}
    if protocol == CODEX_IMAGES:
        if mask is not None:
            raise ValueError("ChatGPT 账号图像接口暂不支持 mask 蒙版，请使用 API Key 的 OpenAI Images 服务。")
        if options.output_format not in {"auto", "png"}:
            raise ValueError("ChatGPT 账号图像接口输出 PNG，格式请选择 auto 或 png。")
        if options.quality not in {"auto", "low", "medium", "high"}:
            raise ValueError("ChatGPT 账号图像接口的质量请选择 auto、low、medium 或 high。")
        if options.size in {"1K", "2K", "4K"} or extra:
            raise ValueError("ChatGPT 账号图像接口使用 auto 或宽x高尺寸，不支持附加请求字段。")
        body.update({key: value for key, value in options.to_dict().items() if key != "output_format"})
        if images:
            body["images"] = [{"image_url": image.data_url} for image in images]
        return {"json": body}
    if protocol == "openai_images":
        if options.size in {"1K", "2K", "4K"}:
            raise ValueError("OpenAI Images 尺寸请使用 auto 或 宽x高。")
        body.update(options.to_dict())
        if options.output_format == "auto":
            body.pop("output_format")
        if not images:
            return {"json": body}
        files = [
            ("image[]", (f"image-{index}.{r.mime.split('/')[1]}", r.data, r.mime)) for index, r in enumerate(images)
        ]
        if mask:
            files.append(("mask", ("mask.png", mask.data, mask.mime)))
        if any(isinstance(value, (dict, list)) for value in extra.values()):
            raise ValueError("OpenAI 图片编辑附加字段必须是简单值，不能是 JSON 对象或数组。")
        return {
            "data": {key: str(value).lower() if isinstance(value, bool) else str(value) for key, value in body.items()},
            "files": files,
        }

    if mask is not None:
        raise ValueError("当前 Qwen / Seedream 图像协议不支持 mask；请使用编辑提示词或 OpenAI Images。")
    if options.quality != "auto" or options.background != "auto":
        raise ValueError("当前 Qwen / Seedream 图像协议不支持质量或背景参数，请设为自动。")

    if protocol == "dashscope_images":
        if len(images) > 3 or any(len(image.data) > 10 * 1024 * 1024 for image in images):
            raise ValueError("Qwen Image 最多支持 3 张参考图，每张不超过 10 MiB。")
        if options.output_format not in {"auto", "png"}:
            raise ValueError("Qwen Image 输出为 PNG；请选择自动或 PNG。")
        if options.size in {"1K", "2K", "4K"}:
            raise ValueError("Qwen Image 尺寸请使用 auto 或 宽x高。")
        parameters = extra.pop("parameters", {})
        if not isinstance(parameters, dict) or {"size", "n"}.intersection(parameters):
            raise ValueError("DashScope parameters 必须为对象，且不能覆盖尺寸和数量。")
        if extra:
            raise ValueError("DashScope 附加图像参数请放入 parameters 对象。")
        parameters = {**parameters, "n": options.n}
        if options.size != "auto":
            parameters["size"] = options.size.replace("x", "*")
        content = [{"image": image.data_url} for image in images] + [{"text": prompt}]
        return {
            "json": {
                "model": profile.model_id,
                "input": {"messages": [{"role": "user", "content": content}]},
                "parameters": parameters,
            }
        }

    if protocol == "seedream_images":
        if len(images) + options.n > 15:
            raise ValueError("Seedream 参考图片和输出图片的总数不能超过 15。")
        if options.output_format == "webp":
            raise ValueError("Seedream 不支持 WebP 输出，请选择自动、PNG 或 JPEG（具体模型需支持）。")
        body["response_format"] = "b64_json"
        if options.size != "auto":
            body["size"] = options.size
        if options.output_format != "auto":
            body["output_format"] = options.output_format
        if images:
            body["image"] = [image.data_url for image in images]
        body["sequential_image_generation"] = "auto" if options.n > 1 else "disabled"
        if options.n > 1:
            body["sequential_image_generation_options"] = {"max_images": options.n}
        return {"json": body}
    raise ValueError(f"未知图像协议：{protocol}")


def image_response_items(result: dict, protocol: str) -> list[dict]:
    if protocol == "dashscope_images":
        output = result.get("output") or {}
        items = []
        for choice in output.get("choices", []):
            if choice.get("finish_reason") not in {None, "", "stop"}:
                raise ValueError("Qwen 图像生成未完成：" + str(choice.get("finish_reason")))
            for content in (choice.get("message") or {}).get("content", []):
                if content.get("image"):
                    items.append({"url": content["image"]})
        return items
    return result.get("data") or []
