"""Tests for the composer's same-origin media stream endpoint (frame picker)."""

import shutil
import tempfile
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.media_library.models import MediaAsset
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

TEMP_MEDIA_ROOT = tempfile.mkdtemp(prefix="bb-test-media-")


def tearDownModule():
    shutil.rmtree(TEMP_MEDIA_ROOT, ignore_errors=True)


@override_settings(MEDIA_ROOT=TEMP_MEDIA_ROOT)
class MediaStreamTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="owner@example.com",
            password="testpass123",
            tos_accepted_at=timezone.now(),
        )
        self.org = Organization.objects.create(name="Test Org")
        self.workspace = Workspace.objects.create(organization=self.org, name="Test Workspace")
        OrgMembership.objects.create(user=self.user, organization=self.org, org_role=OrgMembership.OrgRole.OWNER)
        WorkspaceMembership.objects.create(
            user=self.user,
            workspace=self.workspace,
            workspace_role=WorkspaceMembership.WorkspaceRole.OWNER,
        )
        self.asset = MediaAsset.objects.create(
            workspace=self.workspace,
            uploaded_by=self.user,
            file=ContentFile(b"video-bytes", name="clip.mp4"),
            filename="clip.mp4",
            media_type=MediaAsset.MediaType.VIDEO,
            mime_type="video/mp4",
            file_size=11,
            source="upload",
        )
        self.client.force_login(self.user)

    def _url(self, asset_id):
        return reverse(
            "composer:media_stream",
            kwargs={"workspace_id": self.workspace.id, "asset_id": asset_id},
        )

    def test_streams_bytes_with_mime_type(self):
        response = self.client.get(self._url(self.asset.id))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "video/mp4")
        self.assertEqual(response["Accept-Ranges"], "bytes")
        self.assertEqual(b"".join(response.streaming_content), b"video-bytes")

    def test_range_request_returns_partial_content(self):
        # "video-bytes" -> bytes 2..5 are "deo-"
        response = self.client.get(self._url(self.asset.id), HTTP_RANGE="bytes=2-5")
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response["Content-Range"], "bytes 2-5/11")
        self.assertEqual(response["Content-Length"], "4")
        self.assertEqual(b"".join(response.streaming_content), b"deo-")

    def test_open_ended_range_returns_tail(self):
        response = self.client.get(self._url(self.asset.id), HTTP_RANGE="bytes=6-")
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response["Content-Range"], "bytes 6-10/11")
        self.assertEqual(b"".join(response.streaming_content), b"bytes")

    def test_suffix_range_returns_last_bytes(self):
        response = self.client.get(self._url(self.asset.id), HTTP_RANGE="bytes=-5")
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response["Content-Range"], "bytes 6-10/11")
        self.assertEqual(b"".join(response.streaming_content), b"bytes")

    def test_unsatisfiable_range_returns_416(self):
        response = self.client.get(self._url(self.asset.id), HTTP_RANGE="bytes=99-")
        self.assertEqual(response.status_code, 416)
        self.assertEqual(response["Content-Range"], "bytes */11")

    def test_other_workspace_asset_is_404(self):
        other_ws = Workspace.objects.create(organization=Organization.objects.create(name="Other"), name="Other WS")
        other_asset = MediaAsset.objects.create(
            workspace=other_ws,
            uploaded_by=self.user,
            file=ContentFile(b"x", name="other.mp4"),
            filename="other.mp4",
            media_type=MediaAsset.MediaType.VIDEO,
            mime_type="video/mp4",
            file_size=1,
            source="upload",
        )
        response = self.client.get(self._url(other_asset.id))
        self.assertEqual(response.status_code, 404)

    def test_requires_login(self):
        self.client.logout()
        response = self.client.get(self._url(self.asset.id))
        self.assertEqual(response.status_code, 302)


@override_settings(MEDIA_ROOT=TEMP_MEDIA_ROOT)
class MediaFilmstripTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="owner@example.com",
            password="testpass123",
            tos_accepted_at=timezone.now(),
        )
        self.org = Organization.objects.create(name="Test Org")
        self.workspace = Workspace.objects.create(organization=self.org, name="Test Workspace")
        OrgMembership.objects.create(user=self.user, organization=self.org, org_role=OrgMembership.OrgRole.OWNER)
        WorkspaceMembership.objects.create(
            user=self.user,
            workspace=self.workspace,
            workspace_role=WorkspaceMembership.WorkspaceRole.OWNER,
        )
        self.video = MediaAsset.objects.create(
            workspace=self.workspace,
            uploaded_by=self.user,
            file=ContentFile(b"video-bytes", name="clip.mp4"),
            filename="clip.mp4",
            media_type=MediaAsset.MediaType.VIDEO,
            mime_type="video/mp4",
            file_size=11,
            duration=16.0,
            source="upload",
        )
        self.client.force_login(self.user)

    def _url(self, asset_id):
        return reverse(
            "composer:media_filmstrip",
            kwargs={"workspace_id": self.workspace.id, "asset_id": asset_id},
        )

    @patch("apps.media_library.services.extract_video_frames")
    def test_returns_evenly_spaced_frames(self, mock_extract):
        mock_extract.return_value = [b"jpg"] * 8
        response = self.client.get(self._url(self.video.id))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["duration"], 16.0)
        self.assertEqual(len(data["frames"]), 8)
        # Timestamps are duration*(i+0.5)/8 -> first is 1.0, last is 15.0.
        self.assertEqual(data["frames"][0]["time"], 1.0)
        self.assertEqual(data["frames"][-1]["time"], 15.0)
        self.assertTrue(data["frames"][0]["dataUrl"].startswith("data:image/jpeg;base64,"))
        # The 8 timestamps must be what ffmpeg is asked to extract.
        timestamps = mock_extract.call_args.args[1]
        self.assertEqual(len(timestamps), 8)

    @patch("apps.media_library.services.extract_video_frames")
    def test_all_frames_fail_returns_502(self, mock_extract):
        mock_extract.return_value = [None] * 8
        response = self.client.get(self._url(self.video.id))
        self.assertEqual(response.status_code, 502)

    def test_non_video_asset_is_404(self):
        image = MediaAsset.objects.create(
            workspace=self.workspace,
            uploaded_by=self.user,
            file=ContentFile(b"x", name="pic.png"),
            filename="pic.png",
            media_type=MediaAsset.MediaType.IMAGE,
            mime_type="image/png",
            file_size=1,
            source="upload",
        )
        response = self.client.get(self._url(image.id))
        self.assertEqual(response.status_code, 404)

    @patch("apps.media_library.services.extract_video_metadata")
    @patch("apps.media_library.services.extract_video_frames")
    def test_probes_duration_when_missing(self, mock_extract, mock_meta):
        self.video.duration = 0
        self.video.save(update_fields=["duration"])
        mock_meta.return_value = {"duration_seconds": 8.0}
        mock_extract.return_value = [b"jpg"] * 8
        response = self.client.get(self._url(self.video.id))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["duration"], 8.0)
        mock_meta.assert_called_once()
