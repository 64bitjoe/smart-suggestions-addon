"""Two-backend LLM router: primary Anthropic, secondary OpenAI-compatible.

The secondary covers Ollama and MLX (mlx_lm.server / LM Studio) — all speak
POST {base}/v1/chat/completions.
"""
from __future__ import annotations
import asyncio
import logging

import aiohttp

_LOGGER = logging.getLogger(__name__)


class RouterError(Exception):
    """All configured backends failed."""


class ModelRouter:
    def __init__(
        self,
        anthropic_client=None,
        primary_model: str = "claude-haiku-4-5-20251001",
        secondary_base_url: str = "",
        secondary_model: str = "",
        describe_backend: str = "primary",
    ):
        self._anthropic = anthropic_client
        self._primary_model = primary_model
        self._secondary_base_url = secondary_base_url.rstrip("/")
        self._secondary_model = secondary_model
        self._describe_backend = describe_backend
        self._session: aiohttp.ClientSession | None = None

    def _order(self) -> list[str]:
        order = ["primary", "secondary"]
        if self._describe_backend == "secondary":
            order.reverse()
        return order

    async def complete(self, prompt: str, max_tokens: int = 300) -> str:
        errors: list[str] = []
        for backend in self._order():
            try:
                if backend == "primary" and self._anthropic is not None:
                    return await self._call_primary(prompt, max_tokens)
                if backend == "secondary" and self._secondary_base_url:
                    return await self._call_secondary(prompt, max_tokens)
            except Exception as e:
                errors.append(f"{backend}: {e}")
                _LOGGER.warning("LLM backend %s failed: %s", backend, e)
        raise RouterError("; ".join(errors) or "no backend configured")

    async def _call_primary(self, prompt: str, max_tokens: int) -> str:
        resp = await asyncio.wait_for(
            self._anthropic.messages.create(
                model=self._primary_model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            ),
            timeout=30.0,
        )
        return resp.content[0].text

    async def _call_secondary(self, prompt: str, max_tokens: int) -> str:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        async with self._session.post(
            f"{self._secondary_base_url}/v1/chat/completions",
            json={
                "model": self._secondary_model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
            },
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data["choices"][0]["message"]["content"]

    async def close(self) -> None:
        if self._session:
            await self._session.close()
