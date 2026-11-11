"""Tests for Mastodon publishing with asynchronously processed media.

Mastodon accepts a video upload with 202 and keeps processing it; a status that
attaches it before processing ends is refused with 422. The provider has to
wait for the attachment rather than post straight away and fail, and a retry
has to pick up the upload already being processed rather than start over.
"""

from unittest.mock import MagicMock, patch

import pytest

from apps.publisher.engine import MAX_RETRIES, RETRY_BACKOFF
from providers.base import INTERRUPTED_WAIT_MESSAGE
from providers.exceptions import APIError, PublishError, RateLimitError
from providers.mastodon import (
    IDEMPOTENCY_TTL,
    MEDIA_FAILED_MESSAGE,
    MEDIA_POLL_INTERVAL,
    MEDIA_PROCESSING_TIMEOUT,
    MEDIA_TIMEOUT_MESSAGE,
    UPLOAD_REUSE_TTL,
    MastodonProvider,
)
from providers.types import PostType, PublishContent

INSTANCE = "https://mastodon.example"
NOT_READY = {"error": "Cannot attach files that have not finished processing. Try again in a moment!"}


def _response(status_code: int, body: dict) -> MagicMock:
    return MagicMock(status_code=status_code, json=MagicMock(return_value=body))


class FakeClock:
    """Stands in for the ``time`` module: sleeping advances the monotonic clock."""

    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class DictCache:
    def __init__(self, entries: dict | None = None):
        self.entries = dict(entries or {})
        self.timeouts: dict[str, int] = {}

    def get(self, key):
        return self.entries.get(key)

    def set(self, key, value, timeout):
        self.entries[key] = value
        self.timeouts[key] = timeout


@pytest.fixture
def clock():
    fake = FakeClock()
    # The deadline is taken in the provider, the waiting happens in the base helper.
    with patch("providers.mastodon.time", fake), patch("providers.base.time", fake):
        yield fake


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"\x00" * 16)
    return str(path)


def _provider() -> MastodonProvider:
    return MastodonProvider({"instance_url": INSTANCE})


def _content(*media_files: str, text: str = "hello", **kwargs) -> PublishContent:
    return PublishContent(
        text=text,
        media_files=list(media_files),
        post_type=PostType.VIDEO if media_files else PostType.TEXT,
        **kwargs,
    )


def _calls(mock_request, method: str, path: str) -> list:
    return [c for c in mock_request.call_args_list if c.args[0] == method and c.args[1] == f"{INSTANCE}{path}"]


PUBLISH_KEY = "brightbean-42"


def _cache_key(video: str, publish_key: str = PUBLISH_KEY, position: int = 0) -> str | None:
    from providers.mastodon import _file_digest

    return _provider()._upload_cache_key("tok", publish_key, position, _file_digest(video))


# --- Waiting for processing -------------------------------------------------


@patch.object(MastodonProvider, "_request")
def test_waits_for_processing_media_before_posting(mock_request, clock, video):
    mock_request.side_effect = [
        _response(202, {"id": "m1", "url": None}),
        _response(206, {"id": "m1", "url": None}),
        _response(206, {"id": "m1", "url": None}),
        _response(200, {"id": "m1", "url": "https://files/m1.mp4"}),
        _response(200, {"id": "s1", "url": f"{INSTANCE}/@me/s1"}),
    ]

    result = _provider().publish_post("tok", _content(video))

    assert result.platform_post_id == "s1"
    assert len(_calls(mock_request, "GET", "/api/v1/media/m1")) == 3
    assert clock.sleeps == [MEDIA_POLL_INTERVAL, MEDIA_POLL_INTERVAL]
    status_call = _calls(mock_request, "POST", "/api/v1/statuses")[0]
    assert status_call.kwargs["data"]["media_ids[]"] == ["m1"]


@patch.object(MastodonProvider, "_request")
def test_ready_upload_posts_without_polling(mock_request, clock, video):
    mock_request.side_effect = [
        _response(200, {"id": "m1", "url": "https://files/m1.png"}),
        _response(200, {"id": "s1"}),
    ]

    _provider().publish_post("tok", _content(video))

    assert _calls(mock_request, "GET", "/api/v1/media/m1") == []
    assert clock.sleeps == []


@patch.object(MastodonProvider, "_request")
def test_media_still_processing_at_the_cap_raises_a_retryable_error(mock_request, clock, video):
    upload = _response(202, {"id": "m1", "url": None})
    still_processing = _response(206, {"id": "m1", "url": None})
    mock_request.side_effect = lambda method, url, **kw: upload if method == "POST" else still_processing

    with pytest.raises(PublishError) as excinfo:
        _provider().publish_post("tok", _content(video))

    assert excinfo.value.retryable is True
    # Shown to the user verbatim: no media ID, no retry promise.
    assert str(excinfo.value) == MEDIA_TIMEOUT_MESSAGE
    assert MEDIA_PROCESSING_TIMEOUT <= clock.now < MEDIA_PROCESSING_TIMEOUT + MEDIA_POLL_INTERVAL
    # Never attached media that wasn't ready.
    assert _calls(mock_request, "POST", "/api/v1/statuses") == []


def test_wait_stays_inside_the_other_providers_ceiling():
    # The whole publish cycle waits for its slowest post; Instagram and Threads
    # cap their waits at two minutes.
    assert MEDIA_PROCESSING_TIMEOUT <= 120


@patch.object(MastodonProvider, "_request")
def test_failed_processing_is_permanent(mock_request, clock, video):
    mock_request.side_effect = [
        _response(202, {"id": "m1", "url": None}),
        APIError("422", status_code=422, raw_response={"error": "Error processing thumbnail for uploaded media"}),
    ]

    with pytest.raises(PublishError) as excinfo:
        _provider().publish_post("tok", _content(video))

    # Re-uploading the same file would fail the same way.
    assert excinfo.value.retryable is False
    assert str(excinfo.value) == MEDIA_FAILED_MESSAGE
    assert _calls(mock_request, "POST", "/api/v1/statuses") == []


@pytest.mark.parametrize(
    "error",
    [
        APIError("503", status_code=503),
        RateLimitError("429", retry_after=30),
    ],
)
@patch.object(MastodonProvider, "_request")
def test_transient_error_while_polling_stays_retryable(mock_request, clock, video, error):
    mock_request.side_effect = [_response(202, {"id": "m1", "url": None}), error]

    with pytest.raises(type(error)) as excinfo:
        _provider().publish_post("tok", _content(video))

    assert excinfo.value.retryable is True
    assert _calls(mock_request, "POST", "/api/v1/statuses") == []


@patch.object(MastodonProvider, "_request")
def test_stopping_worker_ends_the_wait_with_a_retryable_error(mock_request, clock, video):
    mock_request.side_effect = [_response(202, {"id": "m1", "url": None})]

    with pytest.raises(PublishError) as excinfo:
        _provider().publish_post("tok", _content(video, should_stop=lambda: True))

    # Rescheduled inside the shutdown grace period instead of killed mid-wait.
    assert excinfo.value.retryable is True
    assert str(excinfo.value) == INTERRUPTED_WAIT_MESSAGE
    # Not even the first poll: a request started after SIGTERM can outlast
    # the grace period on its own.
    assert _calls(mock_request, "GET", "/api/v1/media/m1") == []
    assert _calls(mock_request, "POST", "/api/v1/statuses") == []


@patch.object(MastodonProvider, "_request")
def test_stop_requested_during_a_sleep_sends_no_further_request(mock_request, clock, video):
    mock_request.side_effect = [
        _response(202, {"id": "m1", "url": None}),
        _response(206, {"id": "m1", "url": None}),
    ]

    with pytest.raises(PublishError) as excinfo:
        # The signal arrives while the first sleep is under way.
        _provider().publish_post("tok", _content(video, should_stop=lambda: bool(clock.sleeps)))

    assert str(excinfo.value) == INTERRUPTED_WAIT_MESSAGE
    assert clock.sleeps == [MEDIA_POLL_INTERVAL]
    assert len(_calls(mock_request, "GET", "/api/v1/media/m1")) == 1


# --- Posting the status -----------------------------------------------------


@patch.object(MastodonProvider, "_request")
def test_not_ready_422_on_status_is_retried_until_accepted(mock_request, clock, video):
    mock_request.side_effect = [
        _response(200, {"id": "m1", "url": "https://files/m1.mp4"}),
        APIError("422", status_code=422, raw_response=NOT_READY),
        _response(200, {"id": "s1"}),
    ]

    result = _provider().publish_post("tok", _content(video))

    assert result.platform_post_id == "s1"
    assert len(_calls(mock_request, "POST", "/api/v1/statuses")) == 2


@patch.object(MastodonProvider, "_request")
def test_media_wait_and_status_retries_share_one_budget(mock_request, clock, video):
    def respond(method, url, **kw):
        if url.endswith("/api/v2/media"):
            return _response(202, {"id": "m1", "url": None})
        if method == "GET":
            # Ready only once most of the budget is gone.
            return _response(200 if clock.now >= MEDIA_PROCESSING_TIMEOUT - 10 else 206, {"id": "m1"})
        raise APIError("422", status_code=422, raw_response=NOT_READY)

    mock_request.side_effect = respond

    with pytest.raises(PublishError) as excinfo:
        _provider().publish_post("tok", _content(video))

    assert str(excinfo.value) == MEDIA_TIMEOUT_MESSAGE
    # The 422 loop got what was left, not a fresh two minutes of its own.
    assert clock.now < MEDIA_PROCESSING_TIMEOUT + MEDIA_POLL_INTERVAL


@patch.object(MastodonProvider, "_request")
def test_other_422_on_status_is_not_retried(mock_request, clock):
    mock_request.side_effect = [APIError("422", status_code=422, raw_response={"error": "Text can't be blank"})]

    with pytest.raises(APIError):
        _provider().publish_post("tok", _content())

    assert clock.sleeps == []


@patch.object(MastodonProvider, "_request")
def test_server_without_media_lookup_falls_back_to_posting(mock_request, clock, video):
    # A Mastodon-API-compatible server that accepts uploads with 202 but has no
    # GET /api/v1/media/:id. Polling can't help, the status 422 loop can.
    mock_request.side_effect = [
        _response(202, {"id": "m1", "url": None}),
        APIError("404", status_code=404),
        APIError("422", status_code=422, raw_response=NOT_READY),
        _response(200, {"id": "s1"}),
    ]

    result = _provider().publish_post("tok", _content(video))

    assert result.platform_post_id == "s1"


# --- Reusing uploads across attempts ----------------------------------------


@patch.object(MastodonProvider, "_request")
def test_upload_is_remembered_for_the_next_attempt(mock_request, clock, video):
    cache = DictCache()
    mock_request.side_effect = [
        _response(202, {"id": "m1", "url": None}),
        _response(200, {"id": "m1"}),
        _response(200, {"id": "s1"}),
    ]

    _provider().publish_post("tok", _content(video, upload_cache=cache, idempotency_key=PUBLISH_KEY))

    key = _cache_key(video)
    assert cache.entries == {key: "m1"}
    assert cache.timeouts[key] == UPLOAD_REUSE_TTL
    # The cache key never carries the token itself.
    assert "tok" not in key.split(":")


@patch.object(MastodonProvider, "_request")
def test_retry_resumes_waiting_on_the_earlier_upload(mock_request, clock, video):
    cache = DictCache({_cache_key(video): "m1"})
    mock_request.side_effect = [
        _response(206, {"id": "m1", "url": None}),
        _response(200, {"id": "m1", "url": "https://files/m1.mp4"}),
        _response(200, {"id": "s1"}),
    ]

    result = _provider().publish_post("tok", _content(video, upload_cache=cache, idempotency_key=PUBLISH_KEY))

    assert result.platform_post_id == "s1"
    # No second upload, so no second transcode starting from zero.
    assert _calls(mock_request, "POST", "/api/v2/media") == []
    assert _calls(mock_request, "POST", "/api/v1/statuses")[0].kwargs["data"]["media_ids[]"] == ["m1"]


@patch.object(MastodonProvider, "_request")
def test_remembered_upload_that_is_gone_is_replaced(mock_request, clock, video):
    # Attached by an earlier attempt whose response was lost, or cleaned up.
    cache = DictCache({_cache_key(video): "m-old"})
    mock_request.side_effect = [
        APIError("404", status_code=404),
        _response(200, {"id": "m-new", "url": "https://files/m.png"}),
        _response(200, {"id": "s1"}),
    ]

    _provider().publish_post("tok", _content(video, upload_cache=cache, idempotency_key=PUBLISH_KEY))

    assert len(_calls(mock_request, "POST", "/api/v2/media")) == 1
    assert _calls(mock_request, "POST", "/api/v1/statuses")[0].kwargs["data"]["media_ids[]"] == ["m-new"]
    assert cache.entries[_cache_key(video)] == "m-new"


@patch.object(MastodonProvider, "_request")
def test_same_file_in_another_post_gets_its_own_upload(mock_request, clock, video):
    # Post A's upload is still unattached. Post B carries the same file and may
    # publish concurrently; an attachment belongs to one status only.
    cache = DictCache({_cache_key(video, "brightbean-A"): "m-a"})
    mock_request.side_effect = [_response(200, {"id": "m-b"}), _response(200, {"id": "s-b"})]

    _provider().publish_post("tok", _content(video, upload_cache=cache, idempotency_key="brightbean-B"))

    assert _calls(mock_request, "GET", "/api/v1/media/m-a") == []
    assert _calls(mock_request, "POST", "/api/v1/statuses")[0].kwargs["data"]["media_ids[]"] == ["m-b"]
    assert cache.entries[_cache_key(video, "brightbean-B")] == "m-b"


@patch.object(MastodonProvider, "_request")
def test_same_file_twice_in_one_post_is_uploaded_twice(mock_request, clock, video):
    cache = DictCache()
    mock_request.side_effect = [
        _response(200, {"id": "m1"}),
        _response(200, {"id": "m2"}),
        _response(200, {"id": "s1"}),
    ]

    _provider().publish_post("tok", _content(video, video, upload_cache=cache, idempotency_key=PUBLISH_KEY))

    assert _calls(mock_request, "POST", "/api/v1/statuses")[0].kwargs["data"]["media_ids[]"] == ["m1", "m2"]


@patch.object(MastodonProvider, "_request")
def test_nothing_is_remembered_without_a_publish_key(mock_request, clock, video):
    cache = DictCache()
    mock_request.side_effect = [_response(200, {"id": "m1"}), _response(200, {"id": "s1"})]

    _provider().publish_post("tok", _content(video, upload_cache=cache))

    # No way to scope it to one post, so it must not be shared.
    assert cache.entries == {}


# --- Idempotency ------------------------------------------------------------


def _sent_key(mock_request) -> str:
    return _calls(mock_request, "POST", "/api/v1/statuses")[-1].kwargs["headers"]["Idempotency-Key"]


@patch.object(MastodonProvider, "_request")
def test_same_content_sends_the_same_key_even_with_fresh_media_ids(mock_request, clock, video):
    keys = []
    for media_id in ("m1", "m2"):
        mock_request.side_effect = [_response(200, {"id": media_id}), _response(200, {"id": "s1"})]
        _provider().publish_post("tok", _content(video, idempotency_key="brightbean-42"))
        keys.append(_sent_key(mock_request))

    # A retry re-sends the same request as far as Mastodon is concerned.
    assert keys[0] == keys[1]
    assert keys[0].startswith("brightbean-42-")


@patch.object(MastodonProvider, "_request")
def test_edited_content_gets_a_new_key(mock_request, clock, tmp_path):
    first = tmp_path / "a.png"
    first.write_bytes(b"a")
    second = tmp_path / "b.png"
    second.write_bytes(b"b")

    def send(path, text):
        mock_request.side_effect = [_response(200, {"id": "m"}), _response(200, {"id": "s"})]
        _provider().publish_post("tok", _content(str(path), text=text, idempotency_key="brightbean-42"))
        return _sent_key(mock_request)

    original = send(first, "hello")
    # Otherwise Mastodon answers an edited, rescheduled post with the old status.
    assert send(first, "hello, fixed") != original
    assert send(second, "hello") != original


@patch.object(MastodonProvider, "_request")
def test_no_idempotency_header_without_a_key(mock_request, clock):
    mock_request.return_value = _response(200, {"id": "s1"})

    _provider().publish_post("tok", _content())

    assert _calls(mock_request, "POST", "/api/v1/statuses")[0].kwargs["headers"] is None


def test_retry_ladder_fits_inside_mastodons_idempotency_window():
    # A lost response on the first attempt is only deduplicated while Mastodon
    # remembers the key. Retries reuse the upload, so each one costs at most a
    # full media wait plus re-downloading the file (allowed two minutes here).
    per_retry = MEDIA_PROCESSING_TIMEOUT + 120
    assert sum(RETRY_BACKOFF) + MAX_RETRIES * per_retry < IDEMPOTENCY_TTL
    assert len(RETRY_BACKOFF) >= MAX_RETRIES
