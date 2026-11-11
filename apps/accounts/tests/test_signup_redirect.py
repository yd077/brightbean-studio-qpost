"""SIGNUP_MODE together with CUSTOM_SIGNUP_URL: redirect instead of refuse.

With ``invite`` or ``closed`` and a custom URL set, someone who may not sign up
is sent to that URL — from the signup page and from the Google callback alike,
before any account exists. Invitees in invite mode keep the built-in signup,
and existing users keep getting in.
"""

import copy
import os
import subprocess
import sys
import uuid
from datetime import timedelta

import pytest
from allauth.account.models import EmailAddress
from allauth.core import context
from allauth.socialaccount.helpers import complete_social_login
from allauth.socialaccount.models import SocialAccount, SocialLogin
from django.conf import settings as django_settings
from django.contrib.auth.models import AnonymousUser
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.members.models import Invitation, OrgMembership
from apps.organizations.models import Organization

CUSTOM = "https://example.com/signup"


@pytest.fixture
def custom_signup_url(settings):
    settings.CUSTOM_SIGNUP_URL = CUSTOM


@pytest.fixture
def google(settings):
    """A configured Google app, so the provider resolves in the callback flow."""
    providers = copy.deepcopy(settings.SOCIALACCOUNT_PROVIDERS)
    providers["google"]["APP"] = {"client_id": "test-client", "secret": "test-secret"}
    settings.SOCIALACCOUNT_PROVIDERS = providers


@pytest.fixture
def invitation(db):
    return Invitation.objects.create(
        organization=Organization.objects.create(name="Inviting Org"),
        email="invitee@example.com",
        expires_at=timezone.now() + timedelta(days=7),
    )


def _google_callback(email, session=None):
    """Run allauth's provider-callback flow for a verified Google email."""
    request = RequestFactory().get("/accounts/google/login/callback/")
    SessionMiddleware(lambda r: None).process_request(request)
    MessageMiddleware(lambda r: None).process_request(request)
    request.user = AnonymousUser()
    for key, value in (session or {}).items():
        request.session[key] = value

    account = SocialAccount(provider="google", uid=f"uid-{uuid.uuid4()}", extra_data={"email": email})
    sociallogin = SocialLogin(
        user=User(email=email),
        account=account,
        email_addresses=[EmailAddress(email=email, verified=True, primary=True)],
    )
    sociallogin.state = {"process": "login"}
    with context.request_context(request):
        sociallogin.provider = account.get_provider(request)
        return complete_social_login(request, sociallogin)


def _signup_href(response):
    content = response.content.decode()
    marker = content.index(">Sign up</a>")
    start = content.rindex('href="', 0, marker) + len('href="')
    return content[start : content.index('"', start)]


@pytest.mark.django_db
@pytest.mark.parametrize("mode", ["invite", "closed"])
class TestRedirectsWhenSignupNotAllowed:
    def test_signup_page_redirects(self, settings, client, custom_signup_url, mode):
        settings.SIGNUP_MODE = mode
        response = client.get(reverse("account_signup"))

        assert response.status_code == 302
        assert response.url == CUSTOM

    def test_signup_post_redirects_and_creates_nobody(self, settings, client, custom_signup_url, mode):
        settings.SIGNUP_MODE = mode
        response = client.post(
            reverse("account_signup"), {"email": "new@example.com", "password1": "S3cure-pass-for-tests!"}
        )

        assert response.status_code == 302
        assert response.url == CUSTOM
        assert not User.objects.filter(email="new@example.com").exists()

    def test_new_google_user_redirects_and_creates_nobody(self, settings, custom_signup_url, google, mode):
        """The door the email form alone would leave open: auto-signup in the callback."""
        settings.SIGNUP_MODE = mode
        response = _google_callback("new@gmail.com")

        assert response.status_code == 302
        assert response.url == CUSTOM
        assert not User.objects.filter(email="new@gmail.com").exists()

    def test_existing_user_google_login_is_unaffected(self, settings, custom_signup_url, google, mode):
        settings.SIGNUP_MODE = mode
        user = User.objects.create_user(email="existing@gmail.com", password="pw-for-tests-only")

        response = _google_callback("existing@gmail.com")

        assert response.status_code == 302
        assert response.url != CUSTOM
        assert SocialAccount.objects.filter(user=user, provider="google").exists()

    def test_login_link_points_at_custom_url(self, settings, client, custom_signup_url, mode):
        settings.SIGNUP_MODE = mode
        response = client.get(reverse("account_login"))

        assert _signup_href(response) == CUSTOM


@pytest.mark.django_db
class TestInvitees:
    def _open(self, client, invitation):
        client.get(reverse("members:accept_invite", args=[invitation.token]))

    def test_invite_mode_invitee_keeps_builtin_signup(self, settings, client, custom_signup_url, invitation):
        settings.SIGNUP_MODE = "invite"
        self._open(client, invitation)

        assert _signup_href(client.get(reverse("account_login"))) == "/accounts/signup/"
        response = client.post(
            reverse("account_signup"),
            {"email": "invitee@example.com", "password1": "S3cure-pass-for-tests!"},
        )

        assert response.status_code == 302
        assert response.url != CUSTOM
        user = User.objects.get(email="invitee@example.com")
        assert OrgMembership.objects.filter(user=user, organization=invitation.organization).exists()

    def test_invite_mode_invitee_google_signup(self, settings, custom_signup_url, google, invitation):
        settings.SIGNUP_MODE = "invite"
        response = _google_callback("invitee@gmail.com", session={"pending_invite_token": invitation.token})

        assert response.status_code == 302
        assert response.url != CUSTOM
        user = User.objects.get(email="invitee@gmail.com")
        assert OrgMembership.objects.filter(user=user, organization=invitation.organization).exists()

    def test_closed_mode_invitee_is_sent_to_custom_url(self, settings, client, custom_signup_url, invitation):
        """An invite cannot create an account in closed mode, so the link must not promise one."""
        settings.SIGNUP_MODE = "closed"
        self._open(client, invitation)

        assert _signup_href(client.get(reverse("account_login"))) == CUSTOM
        response = client.get(reverse("account_signup"))
        assert response.status_code == 302
        assert response.url == CUSTOM

    def test_lapsed_invite_is_told_rather_than_redirected(self, settings, client, custom_signup_url, invitation):
        """An invitee whose invite expired since following it should learn that, not land on another site."""
        settings.SIGNUP_MODE = "invite"
        self._open(client, invitation)
        invitation.expires_at = timezone.now() - timedelta(minutes=1)
        invitation.save(update_fields=["expires_at"])

        assert _signup_href(client.get(reverse("account_login"))) == "/accounts/signup/"
        response = client.get(reverse("account_signup"))
        assert response.status_code == 200
        assert b"Your invitation has expired or was already used" in response.content


@pytest.mark.django_db
class TestPendingInviteLookup:
    def test_looked_up_once_per_request(self, settings, invitation, django_assert_num_queries):
        from apps.accounts.signup_links import custom_signup_url, signup_allowed

        settings.SIGNUP_MODE = "invite"
        settings.CUSTOM_SIGNUP_URL = CUSTOM
        request = RequestFactory().get("/")
        SessionMiddleware(lambda r: None).process_request(request)
        request.session["pending_invite_token"] = invitation.token

        with django_assert_num_queries(1):
            assert signup_allowed(request) is True
            assert custom_signup_url(request) == ""

    def test_cache_drops_when_the_token_changes(self, settings, invitation):
        from apps.accounts.signup_links import pending_invite_email

        request = RequestFactory().get("/")
        SessionMiddleware(lambda r: None).process_request(request)
        request.session["pending_invite_token"] = invitation.token
        assert pending_invite_email(request) == "invitee@example.com"

        request.session.pop("pending_invite_token")
        assert pending_invite_email(request) is None


@pytest.mark.django_db
class TestOrgDeletionRedirect:
    def _delete_org_as_owner(self, client):
        owner = User.objects.create_user(email="owner@example.com", password="x", tos_accepted_at=timezone.now())
        client.force_login(owner)
        return client.post(reverse("organizations:settings"), {"action": "delete_organization_now"})

    @pytest.mark.parametrize("mode", ["invite", "closed"])
    def test_goes_to_login_when_signup_would_be_closed(self, settings, client, mode):
        settings.SIGNUP_MODE = mode
        response = self._delete_org_as_owner(client)

        assert response.status_code == 302
        assert response.url == reverse("account_login")

    def test_goes_to_custom_url_when_set(self, settings, client, custom_signup_url):
        settings.SIGNUP_MODE = "closed"
        response = self._delete_org_as_owner(client)

        assert response.url == CUSTOM


@pytest.mark.django_db
class TestWithoutCustomUrl:
    def test_closed_notice_still_shown(self, settings, client):
        settings.SIGNUP_MODE = "closed"
        response = client.get(reverse("account_signup"))

        assert response.status_code == 200
        assert b"Sign-up is closed" in response.content

    def test_open_mode_signup_page_stays_open_with_custom_url(self, settings, client, custom_signup_url):
        settings.SIGNUP_MODE = "open"
        response = client.get(reverse("account_signup"))

        assert response.status_code == 200
        assert b"Sign-up is closed" not in response.content


def _import_settings(**env):
    """Import the base settings in a fresh interpreter with the given env vars."""
    return subprocess.run(
        [sys.executable, "-c", "import config.settings.base"],
        cwd=django_settings.BASE_DIR,
        env={
            **os.environ,
            "APP_URL": "https://studio.example.com",
            "ALLOWED_HOSTS": "studio.example.com,studio-alias.example.org,.example.net",
            **env,
        },
        capture_output=True,
        text=True,
        timeout=60,
    )


class TestLoopGuard:
    @pytest.mark.parametrize(
        "url",
        [
            "/accounts/signup/",
            "/accounts/signup",
            "https://studio.example.com/accounts/signup/",
            "https://Studio.Example.com:443/accounts/signup/",
            "https://studio-alias.example.org/accounts/signup/",
            "https://app.example.net/accounts/signup/",
        ],
    )
    def test_custom_url_back_to_own_signup_refuses_to_start(self, url):
        result = _import_settings(SIGNUP_MODE="invite", CUSTOM_SIGNUP_URL=url)

        assert result.returncode != 0
        assert "ImproperlyConfigured" in result.stderr
        assert "points at Studio's own signup page" in result.stderr

    def test_other_site_signup_path_is_fine(self):
        result = _import_settings(SIGNUP_MODE="closed", CUSTOM_SIGNUP_URL="https://example.com/accounts/signup/")

        assert result.returncode == 0, result.stderr

    def test_open_mode_allows_linking_back(self):
        result = _import_settings(SIGNUP_MODE="open", CUSTOM_SIGNUP_URL="/accounts/signup/")

        assert result.returncode == 0, result.stderr
