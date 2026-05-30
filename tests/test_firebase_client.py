"""Unit tests for clients/firebase.py.

We never hit FCM in tests — `messaging.send_each_for_multicast` is
monkeypatched and `get_firebase_app` is stubbed so no service-account JSON
is required.
"""

from typing import Any
from unittest.mock import MagicMock

import pytest
from firebase_admin import messaging

from app.clients import firebase


@pytest.fixture(autouse=True)
def _stub_app(monkeypatch: pytest.MonkeyPatch) -> None:
    # Skip real Firebase init.
    monkeypatch.setattr(firebase, "get_firebase_app", lambda: None)


def _fake_batch_response(per_token_exceptions: list[Exception | None]) -> MagicMock:
    """Shape a fake BatchResponse mirroring what firebase-admin returns."""
    responses = []
    for exc in per_token_exceptions:
        r = MagicMock()
        r.exception = exc
        r.success = exc is None
        responses.append(r)
    batch = MagicMock()
    batch.responses = responses
    batch.success_count = sum(1 for e in per_token_exceptions if e is None)
    batch.failure_count = sum(1 for e in per_token_exceptions if e is not None)
    return batch


def test_send_push_empty_tokens_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[Any] = []
    monkeypatch.setattr(
        messaging,
        "send_each_for_multicast",
        lambda *_a, **_k: called.append(1) or _fake_batch_response([]),
    )
    result = firebase.send_push(tokens=[], data={"x": "y"}, title="t")
    assert result == firebase.SendResult(0, 0, [])
    assert called == []


def test_send_push_happy_path_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        messaging,
        "send_each_for_multicast",
        lambda *_a, **_k: _fake_batch_response([None, None]),
    )
    result = firebase.send_push(tokens=["tok1", "tok2"], data={"k": "v"}, title="hi")
    assert result.success_count == 2
    assert result.failure_count == 0
    assert result.invalid_tokens == []


def test_send_push_extracts_unregistered_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        messaging,
        "send_each_for_multicast",
        lambda *_a, **_k: _fake_batch_response(
            [messaging.UnregisteredError("device uninstalled"), None]
        ),
    )
    result = firebase.send_push(tokens=["dead", "live"], data={}, title="t")
    assert result.invalid_tokens == ["dead"]
    assert result.success_count == 1
    assert result.failure_count == 1


def test_send_push_extracts_sender_id_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        messaging,
        "send_each_for_multicast",
        lambda *_a, **_k: _fake_batch_response(
            [None, messaging.SenderIdMismatchError("wrong project")]
        ),
    )
    result = firebase.send_push(tokens=["a", "b"], data={}, title="t")
    assert result.invalid_tokens == ["b"]


def test_send_push_ignores_transient_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-token-specific failures (e.g. transient FCM 5xx) should NOT prune devices."""
    monkeypatch.setattr(
        messaging,
        "send_each_for_multicast",
        lambda *_a, **_k: _fake_batch_response([RuntimeError("transient")]),
    )
    result = firebase.send_push(tokens=["x"], data={}, title="t")
    assert result.invalid_tokens == []
    assert result.failure_count == 1


def test_send_push_stringifies_data_values(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def _capture(msg: messaging.MulticastMessage) -> MagicMock:
        captured["msg"] = msg
        return _fake_batch_response([None])

    monkeypatch.setattr(messaging, "send_each_for_multicast", _capture)
    firebase.send_push(tokens=["tok"], data={"n": 42, "s": "hi"}, title="t", body="b")
    msg = captured["msg"]
    assert msg.data == {"n": "42", "s": "hi"}
    assert msg.notification.title == "t"
    assert msg.notification.body == "b"
    assert msg.tokens == ["tok"]
