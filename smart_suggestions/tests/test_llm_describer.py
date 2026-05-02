import pytest
from datetime import datetime, timezone, timedelta
from candidate import Candidate, MinerType
from llm_describer import LlmDescriber, Description


class FakeAnthropic:
    """Mocks anthropic SDK shape: client.messages.create(...) -> Response with .content[0].text"""
    def __init__(self):
        self.calls = 0

    @property
    def messages(self):
        return self

    async def create(self, **kwargs):
        self.calls += 1
        class R:
            content = [type("X", (), {"text": (
                '{"title": "Morning kitchen lights", '
                '"description": "Every weekday at 6:45am you turn on the kitchen lights.", '
                '"automation_yaml": "alias: Morning Kitchen\\ntrigger:\\n  - platform: time\\n    at: 06:45:00\\naction:\\n  - service: light.turn_on\\n    target:\\n      entity_id: light.kitchen"}'
            )})()]
        return R()


@pytest.fixture
def candidate():
    return Candidate(
        miner_type=MinerType.TEMPORAL,
        entity_id="light.kitchen",
        action="turn_on",
        details={"hour": 6, "minute": 45, "weekdays": [0, 1, 2, 3, 4]},
        occurrences=10,
        conditional_prob=0.85,
    )


async def test_describer_returns_parsed_response(candidate, tmp_path):
    fake = FakeAnthropic()
    d = LlmDescriber(client=fake, cache_path=tmp_path / "llm.db")
    await d.init()
    desc = await d.describe(candidate)
    assert isinstance(desc, Description)
    assert "kitchen" in desc.title.lower()
    assert "06:45" in desc.automation_yaml or "6:45" in desc.automation_yaml


async def test_describer_caches_by_signature(candidate, tmp_path):
    fake = FakeAnthropic()
    d = LlmDescriber(client=fake, cache_path=tmp_path / "llm.db")
    await d.init()
    await d.describe(candidate)
    await d.describe(candidate)  # second call should hit cache
    assert fake.calls == 1


async def test_describer_cache_expires_after_ttl(candidate, tmp_path, monkeypatch):
    import llm_describer as mod
    fake = FakeAnthropic()
    d = LlmDescriber(client=fake, cache_path=tmp_path / "llm.db", ttl=timedelta(seconds=1))
    await d.init()
    await d.describe(candidate)
    # Fast-forward by 2s by monkeypatching now()
    real_now = datetime.now(timezone.utc) + timedelta(seconds=2)
    monkeypatch.setattr(mod, "_now", lambda: real_now)
    await d.describe(candidate)
    assert fake.calls == 2
