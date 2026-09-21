"""Bounded image HTTP transport. Protocols never pass through Chat Completions."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import socket
from urllib.parse import urlsplit

import httpx

from pycat.core.content.images import MAX_IMAGE_BYTES, MAX_INPUT_IMAGES, inspect_image
from pycat.core.llm.image_protocols import image_request, image_response_items
from pycat.models.contracts.capability import ImageGenerationOptions
from pycat.models.conversation import Message
from pycat.models.provider import Provider


async def with_cancellation(awaitable, cancel_event=None):
    """Cancel and drain the request when its owning run or Qt job is cancelled."""
    task = asyncio.ensure_future(awaitable)
    try:
        while not task.done():
            if cancel_event is not None and cancel_event.is_set():
                raise asyncio.CancelledError()
            await asyncio.wait({task}, timeout=0.1)
        if cancel_event is not None and cancel_event.is_set():
            raise asyncio.CancelledError()
        return await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def request_image(
    *,
    provider: Provider,
    model: str,
    prompt: str,
    options: ImageGenerationOptions,
    images: list[bytes] | None = None,
    mask: bytes | None = None,
    cancel_event=None,
    timeout: float = 600,
    transport_factory=None,
    headers_resolver=None,
) -> Message:
    if not provider.supports_image_api:
        raise ValueError("当前账号连接尚未提供图像接口；请选择 API Key 服务或 ChatGPT / Codex 登录。")
    if provider.auth_type == "chatgpt" and headers_resolver is None:
        raise ValueError("ChatGPT 图像接口需要应用的账号登录服务。")
    profile = provider.effective_model_profile(model)
    if profile.model_type != "image":
        raise ValueError("请选择图像生成模型。")
    if not prompt.strip() or len(prompt) > 32000:
        raise ValueError("生图提示词必须为 1 到 32000 个字符。")
    source_images = images or []
    if len(source_images) > MAX_INPUT_IMAGES or sum(map(len, source_images)) > 100 * 1024 * 1024:
        raise ValueError("参考图片最多 16 张，总大小不能超过 100 MiB。")
    rasters = [inspect_image(data) for data in source_images]
    mask_image = inspect_image(mask) if mask is not None else None
    if mask_image is not None:
        if (
            not rasters
            or mask_image.mime != "image/png"
            or not mask_image.alpha
            or len(mask_image.data) >= 4 * 1024 * 1024
            or (mask_image.width, mask_image.height) != (rasters[0].width, rasters[0].height)
        ):
            raise ValueError("蒙版必须是带透明通道、与首张原图同尺寸且小于 4 MiB 的 PNG。")
    image_api = provider.effective_image_api
    protocol = image_api.protocol
    kwargs = image_request(profile, prompt, options, rasters, mask_image, protocol=protocol)
    endpoint = image_api.endpoint(provider.api_base, edit=bool(rasters))
    headers = await headers_resolver(provider, model) if headers_resolver else provider.get_headers(model)
    headers = {key: value for key, value in headers.items() if key.lower() not in {"content-type", "content-length", "openai-beta"}}

    async def send():
        transport = transport_factory() if transport_factory else None
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=60), transport=transport) as client:
            async with client.stream("POST", endpoint, headers=headers, **kwargs) as response:
                request_id = response.headers.get("x-codex-imagegen-request-id") or response.headers.get("x-request-id", "")
                payload = await _read_bounded(response, MAX_IMAGE_BYTES * options.n * 4 // 3 + 1024 * 1024)
                if response.is_error:
                    try:
                        error = json.loads(payload).get("error", {})
                        detail = str(error.get("message", "")) if isinstance(error, dict) else str(error)
                    except (ValueError, AttributeError):
                        detail = "服务未返回有效错误信息"
                    raise ValueError(f"图片接口 HTTP {response.status_code}: {detail[:800]}")
        result = json.loads(payload)
        if not isinstance(result, dict):
            raise ValueError("图片接口返回了无效 JSON 结构。")
        if result.get("code") or result.get("error"):
            raise ValueError(
                f"图片接口错误：{str(result.get('code') or result.get('error'))[:300]} {str(result.get('message') or '')[:500]}"
            )
        items = image_response_items(result, protocol)
        if not isinstance(items, list) or not items or len(items) > options.n:
            raise ValueError("图片接口没有返回有效图片。")
        output, errors = [], []
        for item in items:
            encoded = item.get("b64_json") if isinstance(item, dict) else None
            if isinstance(item, dict) and item.get("error"):
                errors.append(str(item["error"])[:500])
                continue
            if isinstance(encoded, str) and encoded and len(encoded) <= MAX_IMAGE_BYTES * 4 // 3 + 4:
                raster = inspect_image(base64.b64decode(encoded, validate=True))
            elif protocol == "dashscope_images" and isinstance(item, dict) and item.get("url"):
                raster = inspect_image(await _download_image(item["url"], transport_factory))
            else:
                raise ValueError("图片接口应返回 b64_json 图片内容。")
            output.append(raster.data_url)
        if not output:
            raise ValueError("图片生成失败：" + "; ".join(errors))
        content = f"已{'编辑' if rasters else '生成'} {len(output)} 张图片。"
        if errors:
            content += " 部分图片失败：" + "; ".join(errors)
        return Message(
            role="assistant",
            content=content,
            images=output,
            metadata={
                "model": model,
                "usage": result.get("usage") or {},
                "image_protocol": protocol,
                "image_errors": errors,
                "request_id": result.get("request_id") or request_id,
                "image_operation": "edit" if rasters else "generate",
            },
        )

    return await with_cancellation(asyncio.wait_for(send(), timeout), cancel_event)


async def _read_bounded(response: httpx.Response, limit: int) -> bytes:
    payload = bytearray()
    async for chunk in response.aiter_bytes():
        payload.extend(chunk)
        if len(payload) > limit:
            raise ValueError("图片响应超过大小上限。")
    return bytes(payload)


async def validate_image_download(url: str) -> None:
    """Only public HTTPS results; never accept local files or service credentials."""
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
    ):
        raise ValueError("图像服务返回的下载地址必须为公开 HTTPS URL。")
    addresses = await asyncio.get_running_loop().getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(address[4][0]).is_global for address in addresses):
        raise ValueError("拒绝访问图像结果中的本地或私有网络地址。")


async def _download_image(url: str, transport_factory) -> bytes:
    await validate_image_download(url)
    transport = transport_factory() if transport_factory else None
    # A fresh client prevents response cookies and API credentials crossing to a CDN.
    async with httpx.AsyncClient(timeout=60, transport=transport, follow_redirects=False) as client:
        async with client.stream("GET", url) as response:
            if response.status_code != 200:
                raise ValueError(f"无法下载生成图片：HTTP {response.status_code}")
            return await _read_bounded(response, MAX_IMAGE_BYTES)
