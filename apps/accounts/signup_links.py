"""Who may sign up, and where "Sign up" links send people.

``SIGNUP_MODE`` decides whether a brand-new account may be created
(``signup_allowed``). ``CUSTOM_SIGNUP_URL`` moves every Studio-generated signup
link (the login page, the unknown-account email, the redirect after an owner
deletes their org) to another page. With ``SIGNUP_MODE=open`` it only moves the
links: ``/accounts/signup/`` stays open, so that page may send people back to
it. With ``invite`` or ``closed``, anyone who may not sign up is also redirected
there from the signup page and the Google callback (``apps.accounts.adapters``).
"""

from django.conf import settings

from apps.members.models import Invitation

_UNSET = object()


def _session_invite_token(request):
    session = getattr(request, "session", None)
    return session.get("pending_invite_token") if session is not None else None


def pending_invite_email(request):
    """Email of the still-open invite whose token is in the session, else None.

    The accept page stores the token on GET (``apps.members.views.accept_invite``);
    signup accepts the invite from it (``apps.accounts.signals``).

    The result is cached on the request against the token, because one signup
    or login request asks several times (the gate, the link, the form's initial
    email). Keying on the token means the cache drops once signup pops it.
    """
    token = _session_invite_token(request)
    if not token:
        return None
    cached = getattr(request, "_pending_invite_email", _UNSET)
    if cached is not _UNSET and cached[0] == token:
        return cached[1]
    invitation = Invitation.objects.filter(
        token=token,
        accepted_at__isnull=True,
    ).first()
    email = invitation.email if invitation and not invitation.is_expired else None
    if request is not None:
        request._pending_invite_email = (token, email)
    return email


def mark_invite_lapsed(request):
    """Record that the session's invite turned out to be used, for the rest of this request."""
    token = _session_invite_token(request)
    if token and request is not None:
        request._pending_invite_email = (token, None)


def has_lapsed_invite(request):
    """Whether the session carries an invite token that no longer works.

    The accept page only stores the token for an open invitation, so this means
    it expired, was accepted or was revoked since the visitor followed the link.
    """
    return bool(_session_invite_token(request)) and pending_invite_email(request) is None


def signup_allowed(request):
    """Whether a brand-new account may be created for this request, per ``SIGNUP_MODE``.

    Only ever asked when an account would actually be created. Signing in
    with an existing account — including a social login that connects to an
    existing user by email — does not pass through here. In invite mode the
    visitor must carry a still-open invitation (see ``pending_invite_email``).
    """
    mode = settings.SIGNUP_MODE
    if mode == "open":
        return True
    if mode == "closed":
        return False
    return pending_invite_email(request) is not None


def custom_signup_url(request):
    """``CUSTOM_SIGNUP_URL``, or "" when the built-in signup should be linked.

    An invitee gets the built-in signup, because only it pre-fills the invited
    email and accepts the invite on signup. Without this, an invitee who picks
    "Log In to Join" and then "Sign up" would lose the invite. Not in closed
    mode, though: there an invite cannot create an account, so the built-in
    signup would only bounce them to ``CUSTOM_SIGNUP_URL`` anyway.

    In invite mode a visitor whose invite has lapsed also stays on Studio, so
    the closed-signup page can tell them so rather than silently sending them
    to another site's signup.
    """
    if not settings.CUSTOM_SIGNUP_URL:
        return ""
    if settings.SIGNUP_MODE != "closed" and pending_invite_email(request):
        return ""
    if settings.SIGNUP_MODE == "invite" and has_lapsed_invite(request):
        return ""
    return settings.CUSTOM_SIGNUP_URL
