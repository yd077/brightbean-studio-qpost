"""Mastodon API v1 provider implementation."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from datetime import datetime
from functools import partial
from urllib.parse import urlencode

from .base import SocialProvider
from .exceptions import APIError, OAuthError, PublishError
from .types import (
    AccountProfile,
    AuthType,
    CommentResult,
    InboxMessage,
    MediaType,
    OAuthTokens,
    PostMetrics,
    PostType,
    PublishContent,
    PublishResult,
    RateLimitConfig,
    ReplyResult,
    UploadCache,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_CHARS = 500

# Mastodon processes video (and large images) after the upload returns: the v2
# media endpoint answers 202 with a null ``url``, and a status attaching media
# that is still processing is refused with HTTP 422. Same inline wait as the
# Instagram, Threads and LinkedIn providers, and the same ~2 minute ceiling: the
# whole publish cycle waits for the slowest post, so a longer wait delays every
# other channel's due posts. Media that takes longer is not lost — the upload is
# remembered and the engine's retry resumes waiting on it instead of uploading
# (and restarting the transcode) again.
MEDIA_POLL_INTERVAL = 5  # seconds
MEDIA_PROCESSING_TIMEOUT = 120  # seconds

# Mastodon deletes unattached media after a day; stay well inside that. A
# remembered upload is re-checked before use either way.
UPLOAD_REUSE_TTL = 6 * 3600  # seconds

# How long Mastodon remembers an Idempotency-Key (PostStatusService, setex 3600).
# A retry after a lost response is only deduplicated inside this window, so the
# engine's whole retry ladder has to fit in it — pinned by a test.
IDEMPOTENCY_TTL = 3600  # seconds

# Mastodon's own wording for the 422 above (media_attachments.validations.not_ready).
_NOT_READY_MARKER = "not finished processing"

# Shown to the user as-is (a PublishError message passes through). The timeout
# is retryable, so neither may promise a retry that stops being true once the
# retries run out.
MEDIA_TIMEOUT_MESSAGE = "Mastodon took too long to process the attached media."
MEDIA_FAILED_MESSAGE = "Mastodon couldn't process the attached media. Check the file and publish again."

# What GET /api/v1/media/:id says about an attachment.
_READY = "ready"
_PROCESSING = "processing"
# Not there to ask about: attached to a status already, deleted, or a server
# that implements the upload API but not this endpoint.
_UNKNOWN = "unknown"


def _file_digest(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


class MastodonProvider(SocialProvider):
    """Mastodon API v1 provider.

    Uses instance-specific OAuth.  The ``credentials`` dict must contain:

    - ``instance_url`` – e.g. ``https://mastodon.social``
    - ``client_id``    – obtained from app registration
    - ``client_secret``– obtained from app registration
    """

    def __init__(self, credentials: dict | None = None):
        super().__init__(credentials)
        instance = self.credentials.get("instance_url", "")
        self.instance_url: str = instance.rstrip("/")

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @property
    def platform_name(self) -> str:
        return "Mastodon"

    @property
    def auth_type(self) -> AuthType:
        return AuthType.INSTANCE_OAUTH

    @property
    def max_caption_length(self) -> int:
        return DEFAULT_MAX_CHARS

    @property
    def supported_post_types(self) -> list[PostType]:
        return [PostType.TEXT, PostType.IMAGE, PostType.VIDEO, PostType.POLL]

    @property
    def supported_media_types(self) -> list[MediaType]:
        return [
            MediaType.JPEG,
            MediaType.PNG,
            MediaType.GIF,
            MediaType.MP4,
            MediaType.MOV,
            MediaType.WEBP,
        ]

    @property
    def required_scopes(self) -> list[str]:
        return ["read", "write", "follow"]

    @property
    def rate_limits(self) -> RateLimitConfig:
        return RateLimitConfig(
            requests_per_hour=100,
            requests_per_day=7200,
            publish_per_day=100,
            extra={"publish_per_3h": 300},
        )

    # ------------------------------------------------------------------
    # App registration
    # ------------------------------------------------------------------

    def register_app(self, instance_url: str, redirect_uri: str) -> dict:
        """Register an OAuth application on a Mastodon instance.

        Returns a dict with ``client_id`` and ``client_secret``.
        This only needs to be called once per instance.
        """
        url = f"{instance_url.rstrip('/')}/api/v1/apps"
        resp = self._request(
            "POST",
            url,
            json={
                "client_name": "Brightbean",
                "redirect_uris": redirect_uri,
                "scopes": " ".join(self.required_scopes),
                "website": "https://brightbean.xyz",
            },
        )
        data = resp.json()
        return {
            "client_id": data["client_id"],
            "client_secret": data["client_secret"],
            "instance_url": instance_url,
        }

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------

    def get_auth_url(self, redirect_uri: str, state: str, code_verifier: str | None = None) -> str:
        """Build the instance-specific OAuth authorization URL."""
        params = {
            "client_id": self.credentials["client_id"],
            "redirect_uri": redirect_uri,
            "scope": " ".join(self.required_scopes),
            "response_type": "code",
            "state": state,
        }
        return f"{self.instance_url}/oauth/authorize?{urlencode(params)}"

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None = None) -> OAuthTokens:
        """Exchange an authorization code for a Mastodon access token."""
        resp = self._request(
            "POST",
            f"{self.instance_url}/oauth/token",
            data={
                "client_id": self.credentials["client_id"],
                "client_secret": self.credentials["client_secret"],
                "code": code,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "scope": " ".join(self.required_scopes),
            },
        )
        data = resp.json()
        if "error" in data:
            raise OAuthError(
                f"Token exchange failed: {data.get('error_description', data['error'])}",
                platform=self.platform_name,
                raw_response=data,
            )
        return OAuthTokens(
            access_token=data["access_token"],
            token_type=data.get("token_type", "Bearer"),
            scope=data.get("scope"),
            raw_response=data,
        )

    def refresh_token(self, refresh_token: str) -> OAuthTokens:
        """Mastodon tokens do not expire by default - return existing token."""
        return OAuthTokens(access_token=refresh_token)

    def revoke_token(self, access_token: str) -> bool:
        """Revoke a Mastodon OAuth token."""
        try:
            self._request(
                "POST",
                f"{self.instance_url}/oauth/revoke",
                data={
                    "client_id": self.credentials["client_id"],
                    "client_secret": self.credentials["client_secret"],
                    "token": access_token,
                },
            )
            return True
        except Exception:
            logger.exception("Failed to revoke Mastodon token")
            return False

    # ------------------------------------------------------------------
    # Instance info
    # ------------------------------------------------------------------

    def get_instance_max_chars(self, access_token: str) -> int:
        """Retrieve the instance's maximum status character limit."""
        try:
            resp = self._request(
                "GET",
                f"{self.instance_url}/api/v2/instance",
                access_token=access_token,
            )
            data = resp.json()
            return int(data.get("configuration", {}).get("statuses", {}).get("max_characters", DEFAULT_MAX_CHARS))
        except Exception:
            return DEFAULT_MAX_CHARS

    # ------------------------------------------------------------------
    # Profile
    # ------------------------------------------------------------------

    def get_profile(self, access_token: str) -> AccountProfile:
        """Fetch the authenticated user's Mastodon profile."""
        resp = self._request(
            "GET",
            f"{self.instance_url}/api/v1/accounts/verify_credentials",
            access_token=access_token,
        )
        data = resp.json()
        return AccountProfile(
            platform_id=data["id"],
            name=data.get("display_name", data.get("username", "")),
            handle=data.get("acct"),
            avatar_url=data.get("avatar"),
            follower_count=data.get("followers_count", 0),
            extra={"username": data.get("username")},
        )

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def publish_post(self, access_token: str, content: PublishContent) -> PublishResult:
        """Publish a status to Mastodon."""
        # Upload media first if present
        media_ids: list[str] = []
        digests: list[str] = []
        processing: list[str] = []
        for position, media_path in enumerate(content.media_files or []):
            digest = _file_digest(media_path)
            cache_key = self._upload_cache_key(access_token, content.idempotency_key, position, digest)
            media_id, state = self._reuse_or_upload(access_token, media_path, cache_key, content.upload_cache)
            media_ids.append(media_id)
            digests.append(digest)
            if state == _PROCESSING:
                processing.append(media_id)

        deadline = time.monotonic() + MEDIA_PROCESSING_TIMEOUT
        for media_id in processing:
            self._poll_until(
                partial(self._media_settled, access_token, media_id),
                deadline=deadline,
                interval=MEDIA_POLL_INTERVAL,
                on_timeout=self._media_timeout,
                should_stop=content.should_stop,
            )

        # Build status params
        params: dict = {}
        if content.text:
            params["status"] = content.text
        if media_ids:
            params["media_ids[]"] = media_ids

        # Visibility
        visibility = content.extra.get("visibility", "public")
        params["visibility"] = visibility

        # Content warning / spoiler text
        if content.extra.get("spoiler_text"):
            params["spoiler_text"] = content.extra["spoiler_text"]

        # Poll
        if content.post_type == PostType.POLL and content.extra.get("poll"):
            poll = content.extra["poll"]
            params["poll[options][]"] = poll.get("options", [])
            params["poll[expires_in]"] = poll.get("expires_in", 86400)
            if poll.get("multiple"):
                params["poll[multiple]"] = "true"
            if poll.get("hide_totals"):
                params["poll[hide_totals]"] = "true"

        # Reply
        if content.extra.get("in_reply_to_id"):
            params["in_reply_to_id"] = content.extra["in_reply_to_id"]

        data = self._create_status(
            access_token,
            params,
            self._idempotency_headers(content.idempotency_key, params, digests),
            deadline,
            content.should_stop,
        )

        return PublishResult(
            platform_post_id=data["id"],
            url=data.get("url"),
            extra=data,
        )

    def publish_comment(self, access_token: str, post_id: str, text: str) -> CommentResult:
        """Reply to an existing Mastodon status."""
        resp = self._request(
            "POST",
            f"{self.instance_url}/api/v1/statuses",
            access_token=access_token,
            data={
                "status": text,
                "in_reply_to_id": post_id,
                "visibility": "public",
            },
        )
        data = resp.json()
        return CommentResult(
            platform_comment_id=data["id"],
            extra=data,
        )

    # ------------------------------------------------------------------
    # Media upload
    # ------------------------------------------------------------------

    def _upload_media(self, access_token: str, file_path: str) -> tuple[str, bool]:
        """Upload a media file. Returns its media ID and whether it is ready to attach.

        200 means processed; 202 means Mastodon accepted the file and is still
        processing it, which is the normal answer for video.
        """
        with open(file_path, "rb") as f:
            resp = self._request(
                "POST",
                f"{self.instance_url}/api/v2/media",
                access_token=access_token,
                files={"file": f},
            )
        data = resp.json()
        return str(data["id"]), resp.status_code == 200

    def _upload_cache_key(self, access_token: str, publish_key: str | None, position: int, digest: str) -> str | None:
        """Where an upload is remembered, or None when it must not be shared.

        Scoped to one publish (the engine's per-PlatformPost key) and one slot
        in it. A Mastodon attachment can belong to only one status, so two
        posts carrying the same file must never be handed the same media ID —
        they may publish concurrently, and the second would lose its media.
        Media IDs also belong to one account on one instance; the token stands
        in for the account and is hashed so it never reaches the cache.
        """
        if not publish_key:
            return None
        account = hashlib.sha256(f"{self.instance_url}\0{access_token}".encode()).hexdigest()[:32]
        publish = hashlib.sha256(publish_key.encode()).hexdigest()[:32]
        return f"mastodon-media:{account}:{publish}:{position}:{digest}"

    def _reuse_or_upload(
        self, access_token: str, file_path: str, key: str | None, cache: UploadCache | None
    ) -> tuple[str, str]:
        """Return a media ID for this file and its state, uploading only if needed.

        An earlier attempt may have uploaded the same bytes and given up while
        Mastodon was still processing them. Reusing that upload means the retry
        waits for the transcode already under way rather than starting a new
        one — otherwise a video that takes longer than one wait never posts.
        """
        cached = cache.get(key) if cache is not None and key is not None else None
        if cached:
            state = self._media_state(access_token, cached)
            if state != _UNKNOWN:
                return cached, state
            # Attached to a status already (an earlier attempt posted, but its
            # response was lost) or cleaned up by the instance: it cannot be
            # attached again. The idempotency key still finds that status.
        media_id, ready = self._upload_media(access_token, file_path)
        if cache is not None and key is not None:
            cache.set(key, media_id, UPLOAD_REUSE_TTL)
        return media_id, _READY if ready else _PROCESSING

    def _media_state(self, access_token: str, media_id: str) -> str:
        """Ask Mastodon whether an attachment has finished processing.

        ``GET /api/v1/media/:id`` answers 206 while processing, 200 once done,
        422 when processing failed, and 404 for media already attached to a
        status (it only looks at unattached media).
        """
        try:
            resp = self._request(
                "GET",
                f"{self.instance_url}/api/v1/media/{media_id}",
                access_token=access_token,
            )
        except APIError as exc:
            if exc.status_code == 422:
                # Re-uploading the same file would fail the same way.
                raise PublishError(
                    MEDIA_FAILED_MESSAGE,
                    platform=self.platform_name,
                    raw_response=exc.raw_response,
                    retryable=False,
                ) from exc
            if exc.status_code in (404, 405, 410, 501):
                return _UNKNOWN
            raise
        return _READY if resp.status_code == 200 else _PROCESSING

    def _media_settled(self, access_token: str, media_id: str) -> bool:
        return self._media_state(access_token, media_id) != _PROCESSING

    def _media_timeout(self) -> PublishError:
        return PublishError(MEDIA_TIMEOUT_MESSAGE, platform=self.platform_name)

    @staticmethod
    def _idempotency_headers(base_key: str | None, params: dict, digests: list[str]) -> dict | None:
        """Mastodon's Idempotency-Key, scoped to this exact content.

        Mastodon remembers a key for an hour and answers a repeat with the
        status it already created, so a retry after a lost response (timeout,
        5xx after the write) cannot post twice. The content is part of the key
        because Mastodon ignores the body on a repeat: a post edited and
        rescheduled inside that hour must publish the edit, not be reported
        as published with what went out before. Media IDs change with every
        upload, so the files' digests stand in for them.
        """
        if not base_key:
            return None
        authored = {k: v for k, v in params.items() if k != "media_ids[]"}
        content = json.dumps({"params": authored, "media": digests}, sort_keys=True)
        return {"Idempotency-Key": f"{base_key}-{hashlib.sha256(content.encode()).hexdigest()[:16]}"}

    def _create_status(
        self,
        access_token: str,
        params: dict,
        headers: dict | None,
        deadline: float,
        should_stop: Callable[[], bool] | None,
    ) -> dict:
        """POST the status, waiting out a 422 for media that is still processing.

        Covers a server that reports readiness a moment before it accepts the
        status, and one whose media endpoint could not be asked at all. The
        422 is raised before anything is written, so re-sending is safe.
        """
        created: dict = {}

        def attempt() -> bool:
            try:
                resp = self._request(
                    "POST",
                    f"{self.instance_url}/api/v1/statuses",
                    access_token=access_token,
                    headers=headers,
                    data=params,
                )
            except APIError as exc:
                if exc.status_code == 422 and _NOT_READY_MARKER in str(exc.raw_response.get("error", "")):
                    return False
                raise
            created.update(resp.json())
            return True

        self._poll_until(
            attempt,
            deadline=deadline,
            interval=MEDIA_POLL_INTERVAL,
            on_timeout=self._media_timeout,
            should_stop=should_stop,
        )
        return created

    # ------------------------------------------------------------------
    # Analytics
    # ------------------------------------------------------------------

    def get_post_metrics(self, access_token: str, post_id: str) -> PostMetrics:
        """Fetch metrics for a Mastodon status."""
        resp = self._request(
            "GET",
            f"{self.instance_url}/api/v1/statuses/{post_id}",
            access_token=access_token,
        )
        data = resp.json()
        return PostMetrics(
            likes=data.get("favourites_count", 0),
            shares=data.get("reblogs_count", 0),
            comments=data.get("replies_count", 0),
            engagements=(data.get("favourites_count", 0) + data.get("reblogs_count", 0) + data.get("replies_count", 0)),
            extra=data,
        )

    # ------------------------------------------------------------------
    # Inbox
    # ------------------------------------------------------------------

    def get_messages(self, access_token: str, since: datetime | None = None) -> list[InboxMessage]:
        """Fetch mentions, favourites, and reblogs from notifications."""
        params: dict = {
            "types[]": ["mention", "favourite", "reblog"],
        }
        if since:
            params["since_id"] = "0"  # Mastodon uses min_id for pagination

        resp = self._request(
            "GET",
            f"{self.instance_url}/api/v1/notifications",
            access_token=access_token,
            params=params,
        )
        data = resp.json()

        messages: list[InboxMessage] = []
        for notif in data:
            account = notif.get("account", {})
            status = notif.get("status", {})
            text = status.get("content", "") if status else ""
            # Strip HTML tags for plain text
            import re

            text = re.sub(r"<[^>]+>", "", text)

            created = notif.get("created_at", "")
            try:
                ts = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                ts = datetime.now()

            messages.append(
                InboxMessage(
                    platform_message_id=notif["id"],
                    sender_id=account.get("id", ""),
                    sender_name=account.get("display_name", account.get("acct", "")),
                    text=text,
                    timestamp=ts,
                    message_type=notif.get("type", "mention"),
                    extra=notif,
                )
            )

        return messages

    def reply_to_comment(self, access_token: str, comment_id: str, text: str, extra: dict | None = None) -> ReplyResult:
        """Reply to a mention. Mastodon answers a notification's status, so this is the same call."""
        return self.reply_to_message(access_token, comment_id, text, extra)

    def reply_to_message(
        self,
        access_token: str,
        message_id: str,
        text: str,
        extra: dict | None = None,
        *,
        human_agent: bool = False,
    ) -> ReplyResult:
        """Reply to a notification's associated status."""
        # Get the notification to find the status ID
        resp = self._request(
            "GET",
            f"{self.instance_url}/api/v1/notifications/{message_id}",
            access_token=access_token,
        )
        notif = resp.json()
        status = notif.get("status", {})
        status_id = status.get("id")

        if not status_id:
            raise PublishError(
                "Cannot reply: notification has no associated status",
                platform=self.platform_name,
            )

        reply_resp = self._request(
            "POST",
            f"{self.instance_url}/api/v1/statuses",
            access_token=access_token,
            data={
                "status": text,
                "in_reply_to_id": status_id,
                "visibility": "public",
            },
        )
        reply_data = reply_resp.json()
        return ReplyResult(
            platform_message_id=reply_data["id"],
            extra=reply_data,
        )
