"""In invite mode one invitation creates at most one account, even under concurrency.

The signup gate only reads the invitation, so two overlapping signups with the
same token could both pass it. The account is therefore created inside a
transaction holding the invitation's row lock, and the invite is accepted in
that same transaction: the loser waits, finds it used, and is rolled back.
"""

import threading
from datetime import timedelta
from unittest.mock import patch

import pytest
from allauth.core.exceptions import ImmediateHttpResponse
from django.contrib.sessions.middleware import SessionMiddleware
from django.db import connection
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from apps.accounts.adapters import create_account_under_invitation
from apps.accounts.models import User
from apps.members.models import Invitation, OrgMembership
from apps.organizations.models import Organization


def _invitation():
    return Invitation.objects.create(
        organization=Organization.objects.create(name="Inviting Org"),
        email="invitee@example.com",
        expires_at=timezone.now() + timedelta(days=7),
    )


def _request(token):
    request = RequestFactory().post("/accounts/signup/")
    SessionMiddleware(lambda r: None).process_request(request)
    request.session["pending_invite_token"] = token
    return request


@pytest.mark.django_db
class TestClaim:
    def test_accepts_the_invite_with_the_new_account(self, settings):
        settings.SIGNUP_MODE = "invite"
        invitation = _invitation()

        user = create_account_under_invitation(
            _request(invitation.token), lambda: User.objects.create_user(email="a@example.com", password="x")
        )

        invitation.refresh_from_db()
        assert invitation.accepted_at is not None
        assert OrgMembership.objects.filter(user=user, organization=invitation.organization).exists()

    def test_used_invite_refuses_without_creating(self, settings):
        settings.SIGNUP_MODE = "invite"
        invitation = _invitation()
        Invitation.objects.filter(pk=invitation.pk).update(accepted_at=timezone.now())
        save_called = []

        with pytest.raises(ImmediateHttpResponse) as exc:
            create_account_under_invitation(_request(invitation.token), lambda: save_called.append(1))

        assert save_called == []
        assert b"Your invitation has expired or was already used" in exc.value.response.content

    def test_used_invite_redirects_to_custom_url(self, settings):
        settings.SIGNUP_MODE = "invite"
        settings.CUSTOM_SIGNUP_URL = "https://example.com/signup"
        invitation = _invitation()
        Invitation.objects.filter(pk=invitation.pk).update(accepted_at=timezone.now())
        # A lapsed invite keeps the visitor on Studio, so they see why.
        with pytest.raises(ImmediateHttpResponse) as exc:
            create_account_under_invitation(_request(invitation.token), lambda: None)

        assert exc.value.response.status_code == 200

    @pytest.mark.parametrize("mode", ["open", "closed"])
    def test_other_modes_just_save(self, settings, mode):
        settings.SIGNUP_MODE = mode

        assert create_account_under_invitation(_request("whatever"), lambda: "saved") == "saved"


@pytest.mark.django_db
class TestSignupFormLosesTheRace:
    def test_invite_used_after_the_gate_is_refused_cleanly(self, settings, client):
        """The gate passed, then another signup used the invite before this one saved."""
        settings.SIGNUP_MODE = "invite"
        invitation = _invitation()
        client.get(reverse("members:accept_invite", args=[invitation.token]))
        Invitation.objects.filter(pk=invitation.pk).update(accepted_at=timezone.now())

        with patch("apps.accounts.adapters.signup_allowed", return_value=True):
            response = client.post(
                reverse("account_signup"),
                {"email": "second@example.com", "password1": "S3cure-pass-for-tests!"},
            )

        assert response.status_code == 200
        assert b"Your invitation has expired or was already used" in response.content
        assert not User.objects.filter(email="second@example.com").exists()


@pytest.mark.django_db(transaction=True)
def test_two_overlapping_signups_create_one_account(settings):
    if connection.vendor != "postgresql":
        pytest.skip("needs real row locks")
    settings.SIGNUP_MODE = "invite"
    invitation = _invitation()
    first_inside = threading.Event()
    release_first = threading.Event()
    outcomes = {}

    def signup(name, email, hold):
        try:

            def save():
                user = User.objects.create_user(email=email, password="x")
                if hold:
                    first_inside.set()
                    release_first.wait(10)
                return user

            create_account_under_invitation(_request(invitation.token), save)
            outcomes[name] = "created"
        except ImmediateHttpResponse:
            outcomes[name] = "refused"
        finally:
            connection.close()

    first = threading.Thread(target=signup, args=("first", "first@example.com", True))
    second = threading.Thread(target=signup, args=("second", "second@example.com", False))
    first.start()
    assert first_inside.wait(10)
    second.start()
    second.join(1)  # blocked on the invitation's row lock
    assert second.is_alive()
    release_first.set()
    first.join(10)
    second.join(10)

    assert outcomes == {"first": "created", "second": "refused"}
    assert list(
        User.objects.filter(email__in=["first@example.com", "second@example.com"]).values_list("email", flat=True)
    ) == ["first@example.com"]
