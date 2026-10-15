import requests

from polymarket.api_client import PolymarketApiClient, ApiError


_DEFAULT = object()


class _FakeResponse:
    def __init__(self, status_code=200, json_data=_DEFAULT, headers=None, text=""):
        self.status_code = status_code
        self._json = json_data if json_data is not _DEFAULT else []
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._json


class _FlakySession:
    """Fails a few times then succeeds, to exercise retry/backoff logic."""

    def __init__(self, fail_times=2, exc=None, final_json=_DEFAULT, status_sequence=None):
        self.calls = 0
        self.fail_times = fail_times
        self.exc = exc or requests.ConnectionError("boom")
        self.final_json = final_json if final_json is not _DEFAULT else [{"ok": True}]
        self.status_sequence = status_sequence

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        if self.status_sequence:
            status = self.status_sequence[min(self.calls - 1, len(self.status_sequence) - 1)]
            if status == 200:
                return _FakeResponse(200, self.final_json)
            return _FakeResponse(status, {}, headers={"Retry-After": "0"} if status == 429 else {})
        if self.calls <= self.fail_times:
            raise self.exc
        return _FakeResponse(200, self.final_json)


def _client(session):
    return PolymarketApiClient(
        data_api_base="https://fake-data-api.example",
        gamma_api_base="https://fake-gamma-api.example",
        clob_api_base="https://fake-clob-api.example",
        timeout=1,
        max_retries=5,
        backoff_base=0.001,
        cache_ttl=0,
        session=session,
    )


def test_retries_on_connection_error_then_succeeds():
    session = _FlakySession(fail_times=2)
    client = _client(session)
    result = client.get_wallet_activity("0xabc")
    assert result == [{"ok": True}]
    assert session.calls == 3


def test_raises_after_exhausting_retries():
    session = _FlakySession(fail_times=999)
    client = PolymarketApiClient(
        data_api_base="https://fake-data-api.example", timeout=1, max_retries=2,
        backoff_base=0.001, cache_ttl=0, session=session,
    )
    try:
        client.get_wallet_activity("0xabc")
        assert False, "expected ApiError"
    except ApiError:
        pass
    assert session.calls == 2


def test_handles_rate_limit_with_retry_after():
    session = _FlakySession(status_sequence=[429, 429, 200])
    client = _client(session)
    result = client.get_wallet_activity("0xabc")
    assert result == [{"ok": True}]
    assert session.calls == 3


def test_handles_server_error_then_succeeds():
    session = _FlakySession(status_sequence=[500, 502, 200])
    client = _client(session)
    result = client.get_wallet_activity("0xabc")
    assert result == [{"ok": True}]


def test_client_error_does_not_retry():
    session = _FlakySession(status_sequence=[404])
    client = _client(session)
    try:
        client.get_wallet_activity("0xabc")
        assert False, "expected ApiError"
    except ApiError:
        pass
    assert session.calls == 1


def test_fetch_wallet_events_never_raises_on_api_failure():
    from polymarket.wallet_activity import fetch_wallet_events

    session = _FlakySession(fail_times=999)
    client = PolymarketApiClient(
        data_api_base="https://fake-data-api.example", timeout=1, max_retries=1,
        backoff_base=0.001, cache_ttl=0, session=session,
    )
    events = fetch_wallet_events(client, "0xabc")
    assert events == []


def test_api_client_handles_unexpected_json_payloads():
    """Verify api_client methods safely return empty collections or None when
    the upstream API returns null, primitives, or non-dict list elements."""
    # Test with payload returning None (JSON null)
    session_none = _FlakySession(fail_times=0, final_json=None)
    client_none = _client(session_none)
    assert client_none.get_wallet_activity("0xabc") == []
    assert client_none.get_wallet_positions("0xabc") == []
    assert client_none.get_wallet_closed_positions("0xabc") == []
    assert client_none.get_leaderboard() == []
    assert client_none.get_market(condition_id="c1") == {}
    assert client_none.get_orderbook(token_id="t1") == {}
    assert client_none.get_wallet_value("0xabc") is None

    # Test with payload returning {"data": null} or non-dict list items
    session_nested = _FlakySession(fail_times=0, final_json={"data": None})
    client_nested = _client(session_nested)
    assert client_nested.get_wallet_activity("0xabc") == []

    # Test with primitive array for value endpoint
    session_prim = _FlakySession(fail_times=0, final_json=[123, "bad"])
    client_prim = _client(session_prim)
    assert client_prim.get_wallet_value("0xabc") is None


def test_parsers_handle_non_dict_payloads():
    """Verify all parsers safely return fallback schemas when given non-dict inputs."""
    from polymarket.parsers import (
        normalize_activity_event, normalize_position, normalize_market,
        normalize_leaderboard_entry, normalize_closed_position,
    )

    for bad_input in (None, 123, "string", []):
        act = normalize_activity_event(bad_input)
        assert isinstance(act, dict)
        assert act["activity_type"] == "TRADE"

        pos = normalize_position(bad_input)
        assert isinstance(pos, dict)

        mkt = normalize_market(bad_input)
        assert isinstance(mkt, dict)
        assert mkt["category"] == "other"

        lb = normalize_leaderboard_entry(bad_input)
        assert isinstance(lb, dict)
        assert lb["wallet"] == ""

        cp = normalize_closed_position(bad_input)
        assert isinstance(cp, dict)
        assert cp["wallet"] == ""
