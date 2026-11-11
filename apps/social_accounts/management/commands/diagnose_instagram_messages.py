"""Inspect the Facebook Login Instagram messaging setup without sending DMs."""

import json

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.publisher.engine import _resolve_publish_credentials
from apps.social_accounts.models import SocialAccount
from providers import get_provider
from providers.meta_messaging import enabled


class Command(BaseCommand):
    help = "Check Instagram DM grants, Page subscription, and conversation readability. Does not send messages."

    def add_arguments(self, parser):
        parser.add_argument("--account-id", required=True)
        parser.add_argument("--json", action="store_true")
        parser.add_argument(
            "--refresh-scopes", action="store_true", help="Save the verified missing-permission list on this account."
        )

    def handle(self, *args, **options):
        try:
            account = SocialAccount.objects.select_related("workspace").get(
                pk=options["account_id"], platform="instagram"
            )
        except (SocialAccount.DoesNotExist, ValueError, ValidationError) as exc:
            raise CommandError("No Facebook Login Instagram account matched.") from exc
        provider = get_provider("instagram", _resolve_publish_credentials(account))
        checks = []

        def check(name, ok, detail):
            checks.append({"name": name, "ok": ok, "detail": detail})

        check("linked Page", bool(account.webhook_target_id), "Reconnect Instagram if the linked Page ID is missing.")
        check(
            "Page messaging task",
            account.messaging_access,
            "Grant Page messaging access and reconnect if denied; unknown requires verification in Meta.",
        )
        check("Human Agent enabled", None, str(enabled(provider.credentials.get("human_agent_enabled"))))
        try:
            granted = provider.get_granted_scopes(account.oauth_access_token)
            missing = sorted(set(provider.required_scopes) - granted) if granted is not None else None
            check(
                "permissions",
                not missing if missing is not None else None,
                ", ".join(missing)
                if missing
                else "Grant verified."
                if granted is not None
                else "Grant could not be inspected.",
            )
            if options["refresh_scopes"] and missing is not None:
                SocialAccount.objects.filter(pk=account.pk).update(missing_scopes=missing)
        except Exception as exc:
            check("permissions", False, f"Readback failed ({type(exc).__name__}).")
        if account.webhook_target_id:
            base = "https://graph.facebook.com/v25.0"
            try:
                data = provider._request(
                    "GET",
                    f"{base}/{account.webhook_target_id}/subscribed_apps",
                    access_token=account.oauth_access_token,
                ).json()
                app_id = str(provider.credentials.get("client_id"))
                subscribed = any(
                    str(item.get("id")) == app_id and "messages" in item.get("subscribed_fields", [])
                    for item in data.get("data", [])
                )
                check(
                    "Page messages subscription",
                    subscribed,
                    "Repair with subscribe_webhooks --platform instagram --retry-failed after reconnecting.",
                )
            except Exception as exc:
                check("Page messages subscription", False, f"Readback failed ({type(exc).__name__}).")
            try:
                provider._request(
                    "GET",
                    f"{base}/{account.webhook_target_id}/conversations",
                    access_token=account.oauth_access_token,
                    params={"platform": "instagram", "fields": "id", "limit": 1},
                )
                check("DM readability", True, "Conversations endpoint accepted the Page token.")
            except Exception as exc:
                check(
                    "DM readability",
                    False,
                    f"Check permissions, Page messaging access, App Review, and Allow access to messages ({type(exc).__name__}).",
                )
        report = {
            "account_id": str(account.id),
            "instagram_id": account.account_platform_id,
            "page_id": account.webhook_target_id,
            "checks": checks,
        }
        if options["json"]:
            self.stdout.write(json.dumps(report))
        else:
            for item in checks:
                status = "ok" if item["ok"] is True else "failed" if item["ok"] is False else "unknown"
                self.stdout.write(f"{item['name']}: {status}. {item['detail']}")
        if any(item["ok"] is False for item in checks):
            raise CommandError("Instagram DM setup has failed checks.")
