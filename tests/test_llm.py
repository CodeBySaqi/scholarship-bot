"""LLM layer tests — run against a real local mock server so the actual HTTP
client, JSON repair, cache and merge logic are all exercised."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from parsers.llm import LLMClient, ParseCache, _clean_payload
from parsers.schema import ScholarshipRecord

sys.path.insert(0, str(Path(__file__).parent))
import mock_llm_server


@pytest.fixture(scope="module")
def server():
    mock_llm_server.serve(port=8799)
    yield "http://127.0.0.1:8799"


@pytest.fixture
def client(server, tmp_path):
    return LLMClient(
        api_key="test",
        base_url=f"{server}/v1",
        model="mock-model",
        max_items_per_call=3,
        budget_usd=1.0,
        retries=0,
    )


class TestTriage:
    def test_clean_record_needs_nothing(self):
        rec = ScholarshipRecord.model_validate(
            {"title": "Award X", "deadline": "2027-01-01", "funding_type": "Full", "degree_level": "Master", "amount_usd": 30000, "country": "Germany"}
        )
        needs, missing = LLMClient.needs_llm(rec)
        assert needs is False and missing == []

    def test_sparse_record_is_sent_to_the_model(self):
        rec = ScholarshipRecord.model_validate({"title": "Award X"})
        needs, missing = LLMClient.needs_llm(rec)
        assert needs is True and set(missing) >= {"funding_type", "degree_level", "country"}

    def test_rolling_deadline_counts_as_known(self):
        rec = ScholarshipRecord.model_validate({"title": "Award X", "deadline_rolling": True, "funding_type": "Full",
                                                 "degree_level": "Master", "country": "Japan"})
        assert LLMClient.needs_llm(rec)[0] is False


class TestClient:
    def test_batch_returns_one_result_per_item(self, client, tmp_path):
        cache = ParseCache(tmp_path / "pc.sqlite3")
        items = [("aaa", "Award A", "Some text about tuition fees"), ("bbb", "Award B", "Some other text")]
        out = client.parse_batch(items, cache)
        assert set(out) == {"aaa", "bbb"}
        assert out["aaa"]["amount_usd"] == 30000.0
        assert out["aaa"]["funding_type"] == "Full"
        assert client.usage.calls == 1  # both items in a single request

    def test_second_run_is_served_from_cache(self, client, tmp_path):
        cache = ParseCache(tmp_path / "pc2.sqlite3")
        items = [("ccc", "Award C", "identical text for hashing")]
        client.parse_batch(items, cache)
        before = client.usage.calls
        out = client.parse_batch(items, cache)
        assert client.usage.calls == before, "cache miss on identical content"
        assert out["ccc"]["notes"] == "written by the mock server"
        assert client.usage.cache_hits == 1

    def test_llm_cannot_overwrite_a_value_we_read_verbatim(self, client, tmp_path):
        """The regex pass found the deadline; the mock says rolling. Regex wins."""
        cache = ParseCache(tmp_path / "pc3.sqlite3")
        rec = ScholarshipRecord.model_validate({"title": "Award D", "deadline": "2027-05-05", "country": "Nepal"})
        items = [("ddd", "Award D", "text")]
        out = client.parse_batch(items, cache)
        merged = ScholarshipRecord.model_validate(out["ddd"]).merged_over(rec)
        # merged_over only fills gaps, so the verified values stay
        assert merged.deadline.isoformat() == "2027-05-05"
        assert merged.country == "Nepal"

    def test_invalid_llm_output_is_dropped_not_stored(self, client, tmp_path, monkeypatch):
        cache = ParseCache(tmp_path / "pc4.sqlite3")
        monkeypatch.setattr(client, "_call", lambda batch: [{"idx": batch[0][0], "deadline": "whenever", "title": "Award X"}])
        out = client.parse_batch([("eee", "Award E", "text")], cache)
        # a bad field is dropped, the row survives
        assert out.get("eee") is not None
        assert out["eee"].get("deadline") != "whenever"

    def test_budget_stops_calls(self, client, tmp_path):
        client.budget_usd = 0.0
        cache = ParseCache(tmp_path / "pc5.sqlite3")
        client.parse_batch([("fff", "F", "text")], cache)
        assert client.usage.calls == 0 and client.usage.budget_exhausted is True

    def test_usage_cost_tracking(self, client, tmp_path):
        cache = ParseCache(tmp_path / "pc6.sqlite3")
        client.parse_batch([("ggg", "G", "x" * 400)], cache)
        assert client.usage.tokens_in > 0
        assert client.usage.cost_usd == 0.0  # unknown model → priced as free/local

    def test_http_error_does_not_raise_out(self, tmp_path):
        c = LLMClient(api_key="k", base_url="http://127.0.0.1:1/v1", model="m", retries=0, budget_usd=1.0)
        out = c.parse_batch([("zzz", "Z", "text")], ParseCache(tmp_path / "pc7.sqlite3"))
        assert out == {}
        assert c.usage.failures >= 1


class TestPayloadHygiene:
    def test_null_strings_become_none(self):
        cleaned = _clean_payload({"a": "N/A", "b": "unknown", "c": "keep", "d": ["x", "", None]})
        assert cleaned == {"a": None, "b": None, "c": "keep", "d": ["x"]}

    def test_markdown_fence_is_stripped(self):
        raw = {"choices": [{"message": {"content": "```json\n{\"results\": []}\n```"}}]}
        assert LLMClient._extract_json(raw) == {"results": []}

    def test_prose_around_json_is_recovered(self):
        raw = {"choices": [{"message": {"content": "Sure!\n{\"results\": [{\"idx\": 1}]}\nHope that helps."}}]}
        assert LLMClient._extract_json(raw)["results"][0]["idx"] == 1

    def test_content_parts_list(self):
        raw = {"choices": [{"message": {"content": [{"text": '{"results": ['}, {"text": "]}"}]}}]}
        assert LLMClient._extract_json(raw) == {"results": []}

    def test_no_choices_returns_none(self):
        assert LLMClient._extract_json({}) is None
