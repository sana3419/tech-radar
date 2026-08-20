"""OpenAI-compatible structured path with a fake client (no network)."""
from types import SimpleNamespace

from techradar.llm import client
from techradar.llm.schemas import EnrichBatchOut, EnrichOut
from techradar.settings import get_settings
from tests.test_ingest import session, UNIQ  # noqa: F401


def _fake_openai(monkeypatch, contents: list[str]):
    calls = {"n": 0, "kw": []}

    class _Comp:
        def create(self, **kw):
            calls["kw"].append(kw)
            text = contents[min(calls["n"], len(contents) - 1)]
            calls["n"] += 1
            usage = SimpleNamespace(prompt_tokens=1000, completion_tokens=100, prompt_cache_hit_tokens=400,
                                    prompt_tokens_details=None)
            return SimpleNamespace(usage=usage, choices=[SimpleNamespace(finish_reason="stop",
                                                                            message=SimpleNamespace(content=text))])

    class _Client:
        def __init__(self, *a, **k):
            self.chat = SimpleNamespace(completions=_Comp())

    import openai
    monkeypatch.setattr(openai, "OpenAI", _Client)
    monkeypatch.setattr(get_settings(), "llm_provider", "openai")
    monkeypatch.setattr(get_settings(), "openai_api_key", "sk-test")
    return calls


GOOD = EnrichBatchOut(items=[EnrichOut(summary_one="一句话", points=["a"], type="tool", domains=["llm"],
                                       stacks=["python"], entities=[], lang="en")]).model_dump_json()


def test_openai_structured_parses_and_accounts(session, monkeypatch):
    calls = _fake_openai(monkeypatch, ["```json\n" + GOOD + "\n```"])
    parsed, meta = client.structured(session, EnrichBatchOut, system="sys", user="u", model="deepseek-chat")
    assert parsed.items[0].summary_one == "一句话"
    assert calls["kw"][0]["response_format"] == {"type": "json_object"}
    assert "JSON Schema" in calls["kw"][0]["messages"][0]["content"]
    assert meta["tokens_in"] == 1000 and meta["cache_read"] == 400 and meta["cost"] > 0


def test_openai_structured_retries_on_bad_json(session, monkeypatch):
    calls = _fake_openai(monkeypatch, ["not json at all", GOOD])
    parsed, meta = client.structured(session, EnrichBatchOut, system="sys", user="u", model="deepseek-chat")
    assert calls["n"] == 2 and parsed.items
    # retry carries the error back to the model
    assert "schema" in calls["kw"][1]["messages"][-1]["content"]


def test_not_configured(session, monkeypatch):
    import pytest
    monkeypatch.setattr(get_settings(), "llm_provider", "openai")
    monkeypatch.setattr(get_settings(), "openai_api_key", None)
    assert not client.is_configured()
    with pytest.raises(client.LLMNotConfigured):
        client.structured(session, EnrichBatchOut, system="s", user="u", model="deepseek-chat")
