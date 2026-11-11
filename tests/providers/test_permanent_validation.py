"""Content a provider refuses before calling out must fail without retries.

The publish engine retries anything not marked ``retryable=False``. A post that
is the wrong shape for the platform is the same shape on every attempt, so a
retry only delays the failure — and, once the budget ran out, used to replace
the one useful sentence ("TikTok only supports VIDEO posts") with "kept
failing ... reconnect the account".
"""

from unittest.mock import patch

import pytest

from providers.bluesky import BlueskyProvider
from providers.devto import DevtoProvider
from providers.exceptions import PublishError
from providers.facebook import FacebookProvider
from providers.google_business import GoogleBusinessProvider
from providers.instagram import InstagramProvider
from providers.instagram_login import InstagramLoginProvider
from providers.pinterest import PinterestProvider
from providers.tiktok import TikTokProvider
from providers.types import PostType, PublishContent
from providers.youtube import YouTubeProvider

IMAGE_URL = "https://cdn.example.com/photo.jpg"

CASES = [
    pytest.param(
        TikTokProvider,
        PublishContent(text="hi", media_urls=[IMAGE_URL], post_type=PostType.TEXT),
        id="tiktok-not-a-video",
    ),
    pytest.param(
        YouTubeProvider,
        PublishContent(text="hi", media_urls=[IMAGE_URL], post_type=PostType.IMAGE),
        id="youtube-not-a-video",
    ),
    pytest.param(
        PinterestProvider,
        PublishContent(text="hi", media_urls=[IMAGE_URL], post_type=PostType.PIN),
        id="pinterest-no-board",
    ),
    pytest.param(
        PinterestProvider,
        PublishContent(text="hi", post_type=PostType.PIN, extra={"board_id": "b-1"}),
        id="pinterest-no-media",
    ),
    pytest.param(
        InstagramProvider,
        PublishContent(text="hi", post_type=PostType.IMAGE),
        id="instagram-no-media",
    ),
    pytest.param(
        InstagramLoginProvider,
        PublishContent(text="hi", post_type=PostType.IMAGE),
        id="instagram-login-no-media",
    ),
    pytest.param(
        FacebookProvider,
        PublishContent(text="hi", post_type=PostType.TEXT),
        id="facebook-no-page",
    ),
    pytest.param(
        BlueskyProvider,
        PublishContent(text="x" * 301, post_type=PostType.TEXT),
        id="bluesky-too-long",
    ),
    pytest.param(
        DevtoProvider,
        PublishContent(text="body", post_type=PostType.ARTICLE),
        id="devto-no-title",
    ),
    pytest.param(
        GoogleBusinessProvider,
        PublishContent(text="x" * 1501, post_type=PostType.TEXT),
        id="google-business-too-long",
    ),
]


@pytest.mark.parametrize(("provider_cls", "content"), CASES)
def test_refused_content_fails_without_retries(provider_cls, content):
    provider = provider_cls({"client_id": "id", "client_secret": "secret"})

    # Refused before any request: a network call here would mean the check
    # moved behind one, and the test would be exercising something else.
    with (
        patch.object(provider, "_request", side_effect=AssertionError("unexpected request")),
        pytest.raises(PublishError) as excinfo,
    ):
        provider.publish_post("token", content)

    assert excinfo.value.retryable is False
