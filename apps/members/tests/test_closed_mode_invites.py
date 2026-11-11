"""With SIGNUP_MODE=closed an invitation cannot create an account.

So inviting an address with no account would send a link that can never be
redeemed; the invite is refused instead. People who already have an account
can still be invited — they accept by logging in.
"""

import pytest
from allauth.account.models import EmailAddress
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.members.models import Invitation, OrgMembership
from apps.members.services import create_invitation, resend_invitation


@pytest.fixture
def owner(db):
    # post_save provisions "My Organization" with this user as owner.
    return User.objects.create_user(email="owner@example.com", password="x", tos_accepted_at=timezone.now())


@pytest.fixture
def org(owner):
    return OrgMembership.objects.get(user=owner).organization


def _invite(org, owner, email):
    return create_invitation(
        org=org,
        email=email,
        org_role=OrgMembership.OrgRole.MEMBER,
        workspace_assignments=[],
        invited_by=owner,
        inviter=owner,
    )


@pytest.mark.django_db
class TestCreateInvitation:
    def test_closed_refuses_address_without_account(self, settings, org, owner):
        settings.SIGNUP_MODE = "closed"

        with pytest.raises(ValueError, match="Sign-up is closed"):
            _invite(org, owner, "stranger@example.com")

        assert not Invitation.objects.filter(email="stranger@example.com").exists()
        assert len(mail.outbox) == 0

    def test_closed_allows_existing_account_case_insensitively(self, settings, org, owner):
        settings.SIGNUP_MODE = "closed"
        User.objects.create_user(email="Colleague@Example.com", password="x")

        invitation = _invite(org, owner, "colleague@example.com")

        assert invitation.pk
        assert len(mail.outbox) == 1

    def test_closed_allows_a_secondary_address(self, settings, org, owner):
        """allauth logs people in by any of their addresses, so an invite to one can be accepted."""
        settings.SIGNUP_MODE = "closed"
        user = User.objects.create_user(email="primary@example.com", password="x")
        EmailAddress.objects.create(user=user, email="secondary@example.com", verified=True, primary=False)

        assert _invite(org, owner, "secondary@example.com").pk

    def test_closed_refuses_resending_an_unredeemable_invite(self, settings, org, owner):
        """An invite created before the switch to closed must not be sent again."""
        invitation = _invite(org, owner, "stranger@example.com")
        mail.outbox.clear()
        Invitation.objects.filter(pk=invitation.pk).update(last_sent_at=None)
        invitation.refresh_from_db()
        settings.SIGNUP_MODE = "closed"

        with pytest.raises(ValueError, match="Sign-up is closed"):
            resend_invitation(invitation)

        assert len(mail.outbox) == 0

    @pytest.mark.parametrize("mode", ["open", "invite"])
    def test_other_modes_invite_anyone(self, settings, org, owner, mode):
        settings.SIGNUP_MODE = mode

        assert _invite(org, owner, "stranger@example.com").pk


@pytest.mark.django_db
def test_members_view_shows_the_reason(settings, client, owner):
    settings.SIGNUP_MODE = "closed"
    client.force_login(owner)

    response = client.post(reverse("members:invite"), {"email": "stranger@example.com"})

    assert response.status_code == 422
    assert b"Sign-up is closed" in response.content
