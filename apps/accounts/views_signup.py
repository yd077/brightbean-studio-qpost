from allauth.account.views import LoginView, SignupView
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.views import SignupView as SocialSignupView
from django.conf import settings

from .signup_links import custom_signup_url, pending_invite_email, signup_allowed


class InvitePrefillSignupView(SignupView):
    """Signup view that pre-fills and locks the email when a pending
    invite token is in the session."""

    def get_initial(self):
        initial = super().get_initial()
        email = pending_invite_email(self.request)
        if email:
            initial["email"] = email
        return initial

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["invited_email_locked"] = bool(pending_invite_email(self.request))
        return ctx

    def form_valid(self, form):
        # allauth only catches ImmediateHttpResponse around completing the
        # signup, not around saving the user, where an invite-mode signup
        # that lost its invitation to a concurrent one is refused.
        try:
            return super().form_valid(form)
        except ImmediateHttpResponse as e:
            return e.response


class InviteClaimSocialSignupView(SocialSignupView):
    """allauth's third-party signup form, refusing cleanly when the invite was used meanwhile.

    Reached when a social auto-signup cannot complete on its own (e.g. the
    provider gave no usable email); see ``InvitePrefillSignupView.form_valid``.
    """

    def form_valid(self, form):
        try:
            return super().form_valid(form)
        except ImmediateHttpResponse as e:
            return e.response


class SignupLinkLoginView(LoginView):
    """Login view whose "Sign up" link honors ``CUSTOM_SIGNUP_URL`` and ``SIGNUP_MODE``.

    allauth's ``signup_url`` (with its ``?next=`` passthrough) is kept when the
    setting is empty or an invite is pending. The link is hidden only when it
    would lead to the closed-signup notice: no custom URL, and this visitor may
    not sign up.
    """

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["signup_url"] = custom_signup_url(self.request) or ctx.get("signup_url")
        ctx["show_signup_link"] = bool(settings.CUSTOM_SIGNUP_URL) or signup_allowed(self.request)
        return ctx
