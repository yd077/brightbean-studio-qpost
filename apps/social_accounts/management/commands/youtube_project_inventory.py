"""Credential source inventory, without exporting credentials or claiming Cloud verification."""

import hashlib
import json
import re

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.credentials.models import PlatformCredential, derive_is_configured, resolve_platform_credentials
from apps.organizations.models import Organization


class Command(BaseCommand):
    help = "Inventory runtime Google credential sources. Project numbers require separate read-only Cloud verification."

    def add_arguments(self, parser):
        parser.add_argument("--environment", required=True, help="The actual environment in which this command runs")

    def handle(self, *args, **options):
        env = settings.PLATFORM_CREDENTIALS_FROM_ENV.get("youtube", {})

        def safe_source(client_id, source, purpose):
            candidate = re.match(r"^(\d+)-.*\.apps\.googleusercontent\.com$", client_id)
            return {
                "environment": options["environment"],
                "credential_source": source,
                "credential_fingerprint": hashlib.sha256(client_id.encode()).hexdigest()[:16] if client_id else None,
                "project_number": None,
                "candidate_project_number": candidate.group(1) if candidate else None,
                "cloud_verification": "unverified; client ID prefix is an inference, not Cloud verification",
                "purpose": purpose,
            }

        rows = [
            safe_source(
                str(env.get("client_id") or ""),
                "PLATFORM_GOOGLE_CLIENT_ID / PLATFORM_GOOGLE_CLIENT_SECRET",
                "YouTube Data, Analytics, upload and comments; also Google Business Profile",
            )
            | {"configured": derive_is_configured("youtube", env)},
            safe_source(
                str(getattr(settings, "GOOGLE_AUTH_CLIENT_ID", "") or ""),
                "GOOGLE_AUTH_CLIENT_ID",
                "BrightBean Google sign-in; not used by the YouTube provider",
            ),
        ]
        for org in Organization.objects.all():
            resolved = resolve_platform_credentials("youtube", org.pk)
            source = (
                "PLATFORM_GOOGLE_CLIENT_ID / PLATFORM_GOOGLE_CLIENT_SECRET"
                if derive_is_configured("youtube", env)
                else "organization PlatformCredential"
            )
            client_id = str(resolved.get("client_id") or "")
            rows.append(
                {
                    **safe_source(
                        client_id,
                        source,
                        "Resolved YouTube credential for this organization",
                    ),
                    "organization_id": str(org.pk),
                    "credential_source": source,
                    "configured": derive_is_configured("youtube", resolved),
                    "organization_override_present": PlatformCredential.objects.filter(
                        organization=org, platform="youtube", is_configured=True
                    ).exists(),
                    "organization_override_active": not derive_is_configured("youtube", env)
                    and derive_is_configured("youtube", resolved),
                }
            )
        self.stdout.write(
            json.dumps(
                {
                    "runtime": rows,
                    "organization_overrides": [
                        safe_source(
                            str((credential.credentials or {}).get("client_id") or ""),
                            "organization PlatformCredential",
                            "YouTube organization override",
                        )
                        | {
                            "organization_id": str(credential.organization_id),
                            "configured": credential.is_configured,
                            "active": credential.is_configured and not derive_is_configured("youtube", env),
                        }
                        for credential in PlatformCredential.objects.filter(platform="youtube")
                    ],
                    "reviewer_reported_project": {
                        "project_number": "594155589966",
                        "source": "quota reviewer report",
                        "cloud_verification": "unverified",
                    },
                },
                indent=2,
            )
        )
