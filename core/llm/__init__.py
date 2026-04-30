"""LLM infrastructure layer.

This package contains focused modules used by ChatService:
- request_builder: Build API messages and request bodies
- http_utils: HTTP/SSE response handling and JSON formatting
- thinking_parser: Parse <think>/<analysis> tags from streaming content

ChatService stays as a thin orchestration facade.
"""
