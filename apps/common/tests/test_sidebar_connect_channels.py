"""Sidebar "Connect channels" suggestions.

Every suggestion row carries the same icon slot so the labels line up. YouTube's
slot holds the connect page's generic video glyph rather than the logo (policy
III.F.2.a), including after the workspace's only YouTube channel was disconnected.
"""

import re

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.social_accounts.models import PlatformVisibility, SocialAccount
from apps.workspaces.models import Workspace

ICON_SLOT = '<div class="relative flex-shrink-0">'
VIDEO_GLYPH = 'd="M15.75 10.5l4.72-4.72'
PLUS_BADGE = '<div class="absolute -bottom-1 -right-1 w-2.5 h-2.5 rounded-full bg-orange-500'


class SidebarConnectChannelsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="owner@example.com",
            password="testpass123",
            tos_accepted_at=timezone.now(),
        )
        self.org = Organization.objects.create(name="Org")
        OrgMembership.objects.create(user=self.user, organization=self.org, org_role=OrgMembership.OrgRole.OWNER)
        self.ws = Workspace.objects.create(organization=self.org, name="WS", timezone="Europe/Berlin")
        WorkspaceMembership.objects.create(
            user=self.user, workspace=self.ws, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
        )
        self.client.force_login(self.user)

    def _suggestion_rows(self):
        resp = self.client.get(reverse("calendar:calendar", kwargs={"workspace_id": self.ws.id}))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        block = html[html.index('<div class="sidebar-connect-channels"') :]
        block = block[: block.index("More channels")]
        rows = re.findall(r'<a [^>]*class="sidebar-nav-item">(.*?)</a>', block, re.S)
        return {re.search(r'<span class="sidebar-nav-label">([^<]+)</span>', r).group(1): r for r in rows}

    def test_youtube_suggestion_keeps_icon_slot_after_disconnect(self):
        # Leaves Instagram, TikTok, YouTube as the three suggestions.
        PlatformVisibility.objects.update_or_create(platform="linkedin_company", defaults={"is_visible": False})
        SocialAccount.objects.create(
            workspace=self.ws,
            platform="youtube",
            account_platform_id="yt-1",
            account_name="Channel",
            connection_status=SocialAccount.ConnectionStatus.DISCONNECTED,
        )
        rows = self._suggestion_rows()
        self.assertIn("YouTube", rows)
        for label, row in rows.items():
            self.assertIn(ICON_SLOT, row, label)
            self.assertIn(PLUS_BADGE, row, label)
        self.assertIn(VIDEO_GLYPH, rows["YouTube"])
        self.assertNotIn("youtube-label", rows["YouTube"])
        self.assertNotIn("icon-red.png", rows["YouTube"])
