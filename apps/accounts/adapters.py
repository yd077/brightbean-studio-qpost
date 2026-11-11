from allauth.account.adapter import DefaultAccountAdapter
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.utils import build_absolute_uri
from django.conf import settings
from django.db import transaction
from django.shortcuts import redirect, render

from apps.accounts.models import OAuthConnection
from apps.accounts.signup_links import custom_signup_url, mark_invite_lapsed, signup_allowed
from apps.common.mail import transactional


def _signup_refused_response(request):
    """What someone who may not sign up gets: ``CUSTOM_SIGNUP_URL``, else the closed notice."""
    custom = custom_signup_url(request)
    if custom:
        return redirect(custom)
    return render(request, "account/signup_closed.html")


def create_account_under_invitation(request, save):
    """Run ``save()``, which creates the account, as the single use of the visitor's invite.

    In invite mode the gate (``is_open_for_signup``) only *reads* the invitation,
    so two overlapping signups carrying the same token would both pass it before
    either accepted the invite — one link, two accounts. Here the invitation row
    is locked for the transaction that creates the account and accepted inside
    it: a second request waits on the lock, then finds the invite used, and its
    account is rolled back. ``apps.accounts.signals`` then only moves the new
    user out of their auto-provisioned org.

    Re-entrant, because a social signup through the form saves via both adapters.
    """
    if settings.SIGNUP_MODE != "invite" or getattr(request, "_creating_invited_account", False):
        return save()

    from apps.members.models import Invitation
    from apps.members.services import accept_invitation

    token = request.session.get("pending_invite_token")
    with transaction.atomic():
        invitation = (
            Invitation.objects.select_for_update().filter(token=token, accepted_at__isnull=True).first()
            if token
            else None
        )
        if invitation is None or invitation.is_expired:
            mark_invite_lapsed(request)
            raise ImmediateHttpResponse(_signup_refused_response(request))
        request._creating_invited_account = True
        try:
            user = save()
        finally:
            request._creating_invited_account = False
        # Skip the email-match check: the session token is proof of delivery,
        # and social logins return a provider-controlled email that often
        # differs from the invited address.
        accept_invitation(invitation, user, require_email_match=False)
    return user


class AccountAdapter(DefaultAccountAdapter):
    """Marks allauth's own mail as transactional and applies ``SIGNUP_MODE``.

    Password resets, email confirmations and login codes are mail a person is
    sitting in front of waiting for. Without this they would carry the default
    ``notification`` class and be subject to the per-recipient cap in
    ``apps.common.mail`` — so a user who had already received their allowance of
    publish-failure notices that hour could not reset their own password. The
    global daily cap still applies; nothing bypasses that.

    ``render_mail`` is the single seam every allauth email passes through, so
    overriding it here covers all of them without touching a template. It also
    points any ``signup_url`` in the context (the unknown-account email sent on
    a password reset for an address with no account) at ``CUSTOM_SIGNUP_URL``.

    ``is_open_for_signup`` gates both doors: the email form, and social signups,
    because ``DefaultSocialAccountAdapter.is_open_for_signup`` delegates here.
    That matters since ``SOCIALACCOUNT_AUTO_SIGNUP`` creates the account during
    the provider callback, without the signup page ever rendering. Someone who
    may not sign up is redirected to ``CUSTOM_SIGNUP_URL`` when it is set, and
    otherwise shown ``account/signup_closed.html``; allauth handles the
    ``ImmediateHttpResponse`` on the signup page and the provider callback.
    ``save_user`` makes the invite that allowed an invite-mode signup single-use
    (``create_account_under_invitation``).
    """

    def is_open_for_signup(self, request):
        if signup_allowed(request):
            return True
        custom = custom_signup_url(request)
        if custom:
            raise ImmediateHttpResponse(redirect(custom))
        return False

    def save_user(self, request, user, form, commit=True):
        if not commit:
            return super().save_user(request, user, form, commit=False)
        return create_account_under_invitation(
            request, lambda: super(AccountAdapter, self).save_user(request, user, form)
        )

    def render_mail(self, template_prefix, email, context, headers=None):
        if "signup_url" in context:
            request = context.get("request")
            custom = custom_signup_url(request)
            if custom:
                context = {**context, "signup_url": build_absolute_uri(request, custom)}
        return super().render_mail(
            template_prefix,
            email,
            context,
            headers={**(headers or {}), **transactional()},
        )


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    """Custom adapter that syncs Google social logins to OAuthConnection."""

    def populate_user(self, request, sociallogin, data):
        """Set user.name from Google profile (custom User model has 'name', not first/last)."""
        user = super().populate_user(request, sociallogin, data)
        first_name = data.get("first_name", "")
        last_name = data.get("last_name", "")
        full_name = f"{first_name} {last_name}".strip()
        if full_name and not user.name:
            user.name = full_name
        return user

    def save_user(self, request, sociallogin, form=None):
        """Create OAuthConnection after saving a new social signup."""
        user = create_account_under_invitation(
            request, lambda: super(SocialAccountAdapter, self).save_user(request, sociallogin, form)
        )
        self._sync_oauth_connection(user, sociallogin)
        return user

    def pre_social_login(self, request, sociallogin):
        """Sync OAuthConnection for returning users and auto-connected accounts."""
        super().pre_social_login(request, sociallogin)
        if sociallogin.is_existing:
            self._sync_oauth_connection(sociallogin.user, sociallogin)

    def _sync_oauth_connection(self, user, sociallogin):
        account = sociallogin.account
        if account.provider != "google":
            return
        provider_email = ""
        for ea in sociallogin.email_addresses:
            provider_email = ea.email
            break
        OAuthConnection.objects.update_or_create(
            provider=OAuthConnection.Provider.GOOGLE,
            provider_user_id=account.uid,
            defaults={"user": user, "provider_email": provider_email},
        )
