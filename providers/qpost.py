"""QPost API integration for YouTube, TikTok, and Instagram."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from typing import Any

import httpx

from .exceptions import APIError, PublishError, RateLimitError
from .types import (
    AccountMetrics,
    AccountProfile,
    PostMetrics,
    PostType,
    PublishContent,
    PublishResult,
    PublishState,
    PublishStatus,
)

logger = logging.getLogger(__name__)

DEFAULT_QPOST_BASE_URL = "https://qpost.dev"
DEFAULT_QPOST_API_KEY = "DVsHKPgAFoGjdsedcssnooAKscmPARLYfpHsideCYfdThmaZMXgzaBqtGCgPaoVr"

QPOST_PLATFORMS = frozenset({"youtube", "tiktok", "instagram", "instagram_login"})

PLATFORM_TO_QPOST_TARGET = {
    "youtube": "YOUTUBE",
    "tiktok": "TIKTOK",
    "instagram": "INSTAGRAM",
    "instagram_login": "INSTAGRAM",
}

PLATFORM_TO_QPOST_ACCOUNT_SLUGS = {
    "youtube": ("google", "youtube"),
    "tiktok": ("tiktok",),
    "instagram": ("instagram", "facebook"),
    "instagram_login": ("instagram", "facebook"),
}

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv"}
IMAGE_MIME_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
VIDEO_MIME_TYPES = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
}


def qpost_base_url() -> str:
    try:
        from django.conf import settings

        url = getattr(settings, "QPOST_BASE_URL", "") or os.environ.get("QPOST_BASE_URL", "")
    except Exception:
        url = os.environ.get("QPOST_BASE_URL", "")
    return (url or DEFAULT_QPOST_BASE_URL).rstrip("/")


def resolve_qpost_api_key(access_token: str | None = None, credentials: dict | None = None) -> str:
    """Return the active QPost API key from credentials, access_token, settings, or default."""
    creds = credentials or {}
    if creds.get("qpost_api_key"):
        return str(creds["qpost_api_key"]).strip()
    if creds.get("api_key"):
        return str(creds["api_key"]).strip()
    try:
        from django.conf import settings

        env_key = getattr(settings, "QPOST_API_KEY", "") or os.environ.get("QPOST_API_KEY", "")
    except Exception:
        env_key = os.environ.get("QPOST_API_KEY", "")
    if env_key:
        return str(env_key).strip()
    if access_token and len(access_token.strip()) >= 32:
        return access_token.strip()
    return DEFAULT_QPOST_API_KEY


def is_qpost_enabled(platform: str, credentials: dict | None = None, access_token: str | None = None) -> bool:
    """Whether ``platform`` should route through QPost instead of direct platform OAuth.

    True for YouTube, TikTok, and Instagram whenever no explicit direct OAuth
    client credentials are passed or when the QPost API key is in use.
    """
    if platform not in QPOST_PLATFORMS:
        return False
    creds = credentials or {}
    # If explicit direct OAuth credentials are provided (e.g. unit tests passing
    # client_id/client_secret or client_key/client_secret directly), keep direct
    # mode unless qpost_api_key is explicitly set.
    if creds.get("qpost_api_key"):
        return True
    has_direct_oauth = bool(
        (creds.get("client_id") and creds.get("client_secret"))
        or (creds.get("app_id") and creds.get("app_secret"))
        or (creds.get("client_key") and creds.get("client_secret"))
    )
    if has_direct_oauth and access_token and access_token != resolve_qpost_api_key(None, None):
        return False
    return bool(resolve_qpost_api_key(access_token, creds))


def _qpost_headers(api_key: str) -> dict[str, str]:
    return {"x-api-key": api_key}


def _qpost_request(
    method: str,
    path: str,
    *,
    api_key: str,
    params: dict | None = None,
    json_body: dict | None = None,
    data: dict | None = None,
    files: list | dict | None = None,
    timeout: float = 120.0,
    platform_label: str = "QPost",
) -> dict[str, Any]:
    url = f"{qpost_base_url()}{path}"
    headers = _qpost_headers(api_key)
    with httpx.Client(timeout=timeout) as client:
        response = client.request(
            method,
            url,
            headers=headers,
            params=params,
            json=json_body,
            data=data,
            files=files,
        )

    try:
        payload = response.json() if response.text else {}
    except Exception:
        payload = {"raw": response.text[:500]}

    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        raise RateLimitError(
            f"QPost rate limit exceeded for {platform_label}: {response.text[:300]}",
            retry_after=int(retry_after) if retry_after and retry_after.isdigit() else None,
            platform=platform_label,
            raw_response=payload if isinstance(payload, dict) else {},
        )

    if response.status_code == 409:
        code = payload.get("code") or payload.get("error") if isinstance(payload, dict) else ""
        if code == "PLATFORM_CONNECTION_REQUIRED" or "PLATFORM_CONNECTION_REQUIRED" in response.text:
            accounts_url = (
                payload.get("accountsUrl", "https://qpost.dev/accounts")
                if isinstance(payload, dict)
                else "https://qpost.dev/accounts"
            )
            raise PublishError(
                f"{platform_label} is not connected in your QPost account yet. "
                f"Connect it at {accounts_url} and try again.",
                platform=platform_label,
                raw_response=payload if isinstance(payload, dict) else {},
                retryable=False,
            )

    if response.status_code >= 400:
        err_msg = (
            (payload.get("error") or payload.get("message") or response.text[:300])
            if isinstance(payload, dict)
            else response.text[:300]
        )
        raise APIError(
            f"{platform_label} (via QPost) API error {response.status_code}: {err_msg}",
            status_code=response.status_code,
            platform=platform_label,
            raw_response=payload if isinstance(payload, dict) else {},
        )

    return payload if isinstance(payload, dict) else {"data": payload}


def list_qpost_accounts(api_key: str) -> list[dict[str, Any]]:
    """Return the connected accounts array from GET /api/accounts."""
    body = _qpost_request("GET", "/api/accounts", api_key=api_key, timeout=30.0)
    accounts = body.get("accounts")
    return accounts if isinstance(accounts, list) else []


def _fetch_tiktok_stats(api_key: str) -> dict[str, Any]:
    """Fetch TikTok account stats from GET /api/accounts/tiktok/stats (best-effort)."""
    try:
        body = _qpost_request("GET", "/api/accounts/tiktok/stats", api_key=api_key, timeout=20.0)
        stats = body.get("stats")
        return stats if isinstance(stats, dict) else {}
    except Exception:
        return {}


def _fetch_recent_creator_nickname(api_key: str, qpost_platform: str) -> str:
    """Look up the creator nickname from the most recent QPost post on that platform."""
    try:
        body = _qpost_request(
            "GET",
            "/api/post",
            api_key=api_key,
            params={"platform": qpost_platform},
            timeout=20.0,
        )
        posts = body.get("posts") or []
        for post in posts:
            for plat in post.get("platforms") or []:
                if plat.get("platform") == qpost_platform:
                    meta = plat.get("platformMetadata") or {}
                    nickname = meta.get("creatorNickname") or meta.get("channelTitle") or meta.get("username")
                    if nickname:
                        return str(nickname)
    except Exception:
        pass
    return ""


def get_qpost_profile(platform: str, api_key: str) -> AccountProfile:
    """Build an AccountProfile for YouTube, TikTok, or Instagram from QPost."""
    accounts = list_qpost_accounts(api_key)
    wanted_slugs = PLATFORM_TO_QPOST_ACCOUNT_SLUGS.get(platform, (platform,))
    matched = next(
        (acc for acc in accounts if str(acc.get("platform", "")).lower() in wanted_slugs),
        None,
    )

    qpost_target = PLATFORM_TO_QPOST_TARGET.get(platform, platform.upper())
    display_names = {
        "youtube": "YouTube",
        "tiktok": "TikTok",
        "instagram": "Instagram",
        "instagram_login": "Instagram",
    }
    base_label = display_names.get(platform, platform.title())

    follower_count = 0
    extra: dict[str, Any] = {"provider": "qpost", "qpost_connected": matched is not None}

    if platform == "tiktok":
        stats = _fetch_tiktok_stats(api_key)
        if stats:
            follower_count = int(stats.get("followers") or 0)
            extra["stats"] = stats

    nickname = _fetch_recent_creator_nickname(api_key, qpost_target)

    if matched:
        account_id = str(matched.get("accountId") or f"qpost-{platform}")
        extra.update(matched)
        name = f"{nickname} ({base_label})" if nickname else f"{base_label} (QPost)"
        handle = f"@{nickname.lower().replace(' ', '')}" if nickname else f"@{platform}_qpost"
        return AccountProfile(
            platform_id=account_id,
            name=name,
            handle=handle,
            avatar_url="",
            follower_count=follower_count,
            extra=extra,
        )

    # Even if the platform hasn't been linked on qpost.dev/accounts yet, return
    # a deterministic profile so the user can add the channel in BrightBean Studio
    # and connect it on https://qpost.dev/accounts whenever ready.
    return AccountProfile(
        platform_id=f"qpost-{platform}",
        name=f"{base_label} (QPost)",
        handle=f"@{platform}_qpost",
        avatar_url="",
        follower_count=follower_count,
        extra=extra,
    )


def build_qpost_platform_metadata(platform: str, content: PublishContent) -> dict[str, Any]:
    """Translate BrightBean Studio PublishContent into QPost's per-platform metadata."""
    extra = content.extra or {}

    if platform == "youtube":
        description = content.description or content.text or ""
        privacy_status = str(extra.get("privacy_status") or extra.get("privacyStatus") or "public").lower()
        if privacy_status not in ("private", "unlisted", "public"):
            privacy_status = "public"
        meta: dict[str, Any] = {
            "categoryId": str(extra.get("category_id") or extra.get("categoryId") or "22"),
            "embeddable": bool(extra.get("embeddable", True)),
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": bool(
                extra.get(
                    "self_declared_made_for_kids",
                    extra.get("made_for_kids", extra.get("selfDeclaredMadeForKids", False)),
                )
            ),
        }
        if description:
            meta["description"] = description[:5000]
        tags = extra.get("tags")
        if tags:
            meta["tags"] = tags
        if extra.get("publishAt"):
            meta["publishAt"] = extra["publishAt"]
        return meta

    if platform == "tiktok":
        raw_privacy = str(extra.get("privacy_level") or extra.get("privacyLevel") or "PUBLIC_TO_EVERYONE").upper()
        if raw_privacy == "FOLLOWER_OF_CREATOR":
            raw_privacy = "MUTUAL_FOLLOW_FRIENDS"
        if raw_privacy not in ("PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "SELF_ONLY"):
            raw_privacy = "PUBLIC_TO_EVERYONE"

        allow_comment = (
            bool(extra["allowComment"])
            if "allowComment" in extra
            else not bool(extra.get("disable_comment", False))
        )
        allow_duet = (
            bool(extra["allowDuet"])
            if "allowDuet" in extra
            else not bool(extra.get("disable_duet", False))
        )
        allow_stitch = (
            bool(extra["allowStitch"])
            if "allowStitch" in extra
            else not bool(extra.get("disable_stitch", False))
        )
        auto_add_music = bool(extra.get("autoAddMusic", extra.get("auto_add_music", True)))

        cover_ms = 1000
        raw_cover = extra.get("video_cover_timestamp_ms", extra.get("videoCoverTimestampMs", 1000))
        with contextlib.suppress(TypeError, ValueError):
            cover_ms = max(0, int(raw_cover))

        selections: list[str] = list(extra.get("commercialContentSelections") or [])
        if not selections:
            if extra.get("brand_organic_toggle"):
                selections.append("YOUR_BRAND")
            if extra.get("brand_content_toggle"):
                selections.append("BRANDED_CONTENT")
        commercial_enabled = bool(
            extra.get("commercialContentEnabled")
            or extra.get("brand_organic_toggle")
            or extra.get("brand_content_toggle")
            or selections
        )

        return {
            "privacyLevel": raw_privacy,
            "allowComment": allow_comment,
            "allowDuet": allow_duet,
            "allowStitch": allow_stitch,
            "autoAddMusic": auto_add_music,
            "videoCoverTimestampMs": cover_ms,
            "commercialContentEnabled": commercial_enabled,
            "commercialContentSelections": selections,
            "isAigc": bool(extra.get("is_aigc", extra.get("isAigc", False))),
        }

    # Instagram / Instagram Login
    is_video = content.is_video(0)
    media_count = max(len(content.media_files or []), len(content.media_urls or []))
    if content.post_type == PostType.STORY:
        media_type = "STORIES"
        product_type = "STORIES"
    elif content.post_type in (PostType.REEL, PostType.VIDEO) or (media_count == 1 and is_video):
        media_type = "REELS"
        product_type = "REELS"
    elif content.post_type == PostType.CAROUSEL or media_count > 1:
        media_type = "CAROUSEL"
        product_type = "FEED"
    else:
        media_type = "IMAGE"
        product_type = "FEED"

    ig_meta: dict[str, Any] = {
        "mediaType": extra.get("mediaType") or media_type,
        "mediaProductType": extra.get("mediaProductType") or product_type,
    }
    if "disable_comments" in extra or "disableComments" in extra:
        ig_meta["disableComments"] = bool(extra.get("disableComments", extra.get("disable_comments", False)))
    if extra.get("alt_text") or extra.get("altText"):
        ig_meta["altText"] = str(extra.get("altText") or extra.get("alt_text"))[:1000]
    if extra.get("thumb_offset") is not None or extra.get("thumbOffset") is not None:
        with contextlib.suppress(TypeError, ValueError):
            ig_meta["thumbOffset"] = int(extra.get("thumbOffset", extra.get("thumb_offset")))
    if extra.get("locationId") or extra.get("location_id"):
        ig_meta["locationId"] = str(extra.get("locationId") or extra.get("location_id"))
    if extra.get("userTags"):
        ig_meta["userTags"] = extra["userTags"]
    return ig_meta


def _guess_mime(filepath: str, is_video: bool) -> str:
    ext = os.path.splitext(filepath)[1].lower()
    if is_video:
        return VIDEO_MIME_TYPES.get(ext, "video/mp4")
    return IMAGE_MIME_TYPES.get(ext, "image/jpeg")


def _ensure_local_media_files(content: PublishContent) -> tuple[list[str], list[str]]:
    """Return ``(local_paths, temp_paths_to_cleanup)`` from PublishContent."""
    if content.media_files:
        return list(content.media_files), []

    temp_paths: list[str] = []
    try:
        for idx, url in enumerate(content.media_urls or []):
            is_vid = content.is_video(idx)
            ext = os.path.splitext(url.split("?", 1)[0])[1].lower()
            if not ext:
                ext = ".mp4" if is_vid else ".jpg"
            tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)  # noqa: SIM115
            tmp.close()
            temp_paths.append(tmp.name)
            with httpx.Client(timeout=120.0, follow_redirects=True) as client:
                resp = client.get(url)
                resp.raise_for_status()
                with open(tmp.name, "wb") as f:
                    f.write(resp.content)
        return temp_paths, temp_paths
    except Exception as exc:
        for p in temp_paths:
            with contextlib.suppress(OSError):
                os.unlink(p)
        raise PublishError(
            f"Could not download media for QPost upload: {exc}",
            platform="QPost",
            retryable=True,
        ) from exc


def publish_via_qpost(
    platform: str,
    api_key: str,
    content: PublishContent,
    *,
    platform_label: str = "QPost",
) -> PublishResult:
    """Publish a video or image post to YouTube, TikTok, or Instagram via POST /api/post."""
    qpost_target = PLATFORM_TO_QPOST_TARGET.get(platform, platform.upper())
    metadata = build_qpost_platform_metadata(platform, content)

    caption = (content.text or content.description or content.title or "").strip()
    if not caption:
        caption = content.title.strip() if content.title else "New post"

    title = (content.title or "").strip()
    if platform == "youtube":
        if not title:
            title = caption[:100]
        if content.post_type == PostType.SHORT and "#Shorts" not in title:
            title = f"{title[:90].rstrip()} #Shorts".strip()

    local_paths, cleanup_paths = _ensure_local_media_files(content)
    if not local_paths:
        raise PublishError(
            f"{platform_label} requires a video or image file to publish via QPost.",
            platform=platform_label,
            retryable=False,
        )

    opened_files = []
    try:
        first_ext = os.path.splitext(local_paths[0])[1].lower()
        is_video = content.is_video(0) or first_ext in VIDEO_EXTENSIONS or content.post_type in (
            PostType.VIDEO,
            PostType.REEL,
            PostType.SHORT,
        )

        multipart_files = []
        if is_video:
            path = local_paths[0]
            fobj = open(path, "rb")  # noqa: SIM115
            opened_files.append(fobj)
            filename = os.path.basename(path) or "video.mp4"
            multipart_files.append(("video", (filename, fobj, _guess_mime(path, True))))
        else:
            for idx, path in enumerate(local_paths):
                fobj = open(path, "rb")  # noqa: SIM115
                opened_files.append(fobj)
                filename = os.path.basename(path) or f"image_{idx + 1}.jpg"
                multipart_files.append(("images", (filename, fobj, _guess_mime(path, False))))

        form_data: dict[str, str] = {
            "caption": caption,
            "platforms": json.dumps([{"platform": qpost_target, "metadata": metadata}]),
        }
        if title:
            form_data["title"] = title[:100]

        body = _qpost_request(
            "POST",
            "/api/post",
            api_key=api_key,
            data=form_data,
            files=multipart_files,
            timeout=300.0,
            platform_label=platform_label,
        )
    finally:
        for fobj in opened_files:
            with contextlib.suppress(Exception):
                fobj.close()
        for path in cleanup_paths:
            with contextlib.suppress(OSError):
                os.unlink(path)

    post_obj = body.get("post") if isinstance(body.get("post"), dict) else body
    post_id = str(post_obj.get("id") or "")
    platforms_list = post_obj.get("platforms") or []
    plat_entry = next(
        (p for p in platforms_list if p.get("platform") == qpost_target),
        platforms_list[0] if platforms_list else {},
    )

    pub_status = str(plat_entry.get("publishStatus") or post_obj.get("status") or "").upper()
    error_message = plat_entry.get("errorMessage") or body.get("error")
    if pub_status == "FAILED":
        raise PublishError(
            f"{platform_label} publish failed via QPost: {error_message or 'Unknown error'}",
            platform=platform_label,
            raw_response=body,
            retryable=False,
        )

    post_url = plat_entry.get("postUrl") or post_obj.get("postUrl") or None
    return PublishResult(
        platform_post_id=post_id or str(plat_entry.get("id") or "qpost-published"),
        url=post_url,
        extra=body,
    )


def check_qpost_publish_status(
    platform: str,
    api_key: str,
    handle: str,
    *,
    platform_label: str = "QPost",
) -> PublishStatus:
    """Check publish status of a QPost post via GET /api/post/{id}."""
    qpost_target = PLATFORM_TO_QPOST_TARGET.get(platform, platform.upper())
    body = _qpost_request(
        "GET",
        f"/api/post/{handle}",
        api_key=api_key,
        timeout=30.0,
        platform_label=platform_label,
    )
    post_obj = body.get("post") if isinstance(body.get("post"), dict) else body
    platforms_list = post_obj.get("platforms") or []
    plat_entry = next(
        (p for p in platforms_list if p.get("platform") == qpost_target),
        platforms_list[0] if platforms_list else {},
    )
    pub_status = str(plat_entry.get("publishStatus") or post_obj.get("status") or "").upper()
    if pub_status in ("PUBLISHED", "POSTED", "COMPLETE"):
        return PublishStatus(
            state=PublishState.COMPLETE,
            platform_post_id=str(post_obj.get("id") or handle),
            raw=body,
        )
    if pub_status == "FAILED":
        err = plat_entry.get("errorMessage") or "QPost reported publish failure."
        return PublishStatus(
            state=PublishState.FAILED,
            error=str(err),
            raw=body,
        )
    return PublishStatus(
        state=PublishState.PENDING,
        platform_post_id=str(post_obj.get("id") or handle),
        raw=body,
    )


def get_qpost_post_metrics(
    platform: str,
    api_key: str,
    post_id: str,
    *,
    platform_label: str = "QPost",
) -> PostMetrics:
    """Fetch per-post engagement stats via GET /api/post/{id}/stats."""
    if not post_id:
        return PostMetrics()
    qpost_target = PLATFORM_TO_QPOST_TARGET.get(platform, platform.upper())
    try:
        body = _qpost_request(
            "GET",
            f"/api/post/{post_id}/stats",
            api_key=api_key,
            timeout=30.0,
            platform_label=platform_label,
        )
    except Exception:
        return PostMetrics()

    platforms_list = body.get("platforms") or []
    plat_entry = next(
        (p for p in platforms_list if p.get("platform") == qpost_target),
        platforms_list[0] if platforms_list else {},
    )
    stats = plat_entry.get("stats") or {}
    if not isinstance(stats, dict):
        return PostMetrics()

    return PostMetrics(
        video_views=int(stats.get("views", stats.get("viewCount", 0)) or 0),
        likes=int(stats.get("likes", stats.get("likeCount", 0)) or 0),
        comments=int(stats.get("comments", stats.get("commentCount", 0)) or 0),
        shares=int(stats.get("shares", stats.get("shareCount", 0)) or 0),
        extra={"postUrl": plat_entry.get("postUrl"), "raw": plat_entry},
    )


def get_qpost_account_metrics(platform: str, api_key: str) -> AccountMetrics:
    """Fetch account-level stats from QPost (TikTok stats endpoint)."""
    if platform == "tiktok":
        stats = _fetch_tiktok_stats(api_key)
        if stats:
            return AccountMetrics(
                followers=int(stats.get("followers") or 0),
                extra=stats,
            )
    return AccountMetrics(followers=None)


def get_qpost_tiktok_creator_info(api_key: str) -> dict[str, Any]:
    """Return TikTok creator info compatible with BrightBean Studio's composer."""
    nickname = _fetch_recent_creator_nickname(api_key, "TIKTOK") or "QPost TikTok"
    return {
        "creator_nickname": nickname,
        "privacy_level_options": [
            "PUBLIC_TO_EVERYONE",
            "MUTUAL_FOLLOW_FRIENDS",
            "SELF_ONLY",
        ],
        "comment_disabled": False,
        "duet_disabled": False,
        "stitch_disabled": False,
        "max_video_post_duration_sec": 3600,
    }
