# smart_suggestions/src/automation_builder.py
"""Generate HA automation YAML via AI, create via HAClient REST."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from ha_client import HAClient

_LOGGER = logging.getLogger(__name__)


def _build_automation_prompt(ctx: dict) -> str:
    entity_id = ctx.get("entity_id", "")
    name = ctx.get("name", entity_id)
    typical_time = ctx.get("typical_time", "18:00")
    days = ctx.get("days", [])
    days_str = ", ".join(days) if days else "daily"
    weekday_list = "[" + ", ".join(d.lower() for d in days) + "]" if days else "[]"

    return f"""Generate a valid Home Assistant automation YAML to activate the scene '{name}' ({entity_id}) at {typical_time} on {days_str}.

Return ONLY the raw YAML (no markdown code blocks, no explanation):

alias: {name} — Auto
trigger:
  - platform: time
    at: "{typical_time}:00"
condition:
  - condition: time
    weekday: {weekday_list}
action:
  - service: scene.turn_on
    target:
      entity_id: {entity_id}
mode: single

Adjust the alias and logic to be clean and production-ready. Return only YAML."""


class AutomationBuilder:
    def __init__(self, ai_provider: str, ai_api_key: str, ai_model: str, ai_base_url: str = "") -> None:
        self._provider = ai_provider
        self._model = ai_model
        self._client: Any = None
        if ai_api_key:
            self._init_client(ai_provider, ai_api_key, ai_base_url)

    def _init_client(self, provider: str, api_key: str, base_url: str = "") -> None:
        try:
            if provider == "anthropic":
                import anthropic
                self._client = anthropic.Anthropic(api_key=api_key)
            elif provider == "openai_compatible":
                import openai
                self._client = openai.OpenAI(api_key=api_key, base_url=base_url or None)
        except ImportError as e:
            _LOGGER.error("AutomationBuilder: could not import SDK: %s", e)

    async def build(self, automation_context: dict, ha_client: "HAClient") -> dict:
        """Generate automation YAML and create it in HA. Returns result dict."""
        if not self._client:
            return {"success": False, "error": "No AI client configured — check ai_api_key", "yaml": ""}

        prompt = _build_automation_prompt(automation_context)
        try:
            raw_yaml = await asyncio.get_running_loop().run_in_executor(
                None, self._call_api, prompt
            )
        except Exception as e:
            _LOGGER.error("AutomationBuilder: AI call failed: %s", e)
            return {"success": False, "error": str(e), "yaml": ""}

        # Parse YAML to dict for HA REST API
        try:
            import yaml
            config_dict = yaml.safe_load(raw_yaml)
            if not isinstance(config_dict, dict):
                raise ValueError("YAML did not produce a dict")
        except Exception as e:
            _LOGGER.error("AutomationBuilder: YAML parse error: %s", e)
            return {"success": False, "error": f"YAML parse error: {e}", "yaml": raw_yaml}

        result = await ha_client.create_automation(config_dict)
        if not result.get("success"):
            result["yaml"] = raw_yaml
        return result

    def _call_api(self, prompt: str) -> str:
        if self._provider == "anthropic":
            message = self._client.messages.create(
                model=self._model,
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            )
            return message.content[0].text
        else:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=512,
            )
            return response.choices[0].message.content
