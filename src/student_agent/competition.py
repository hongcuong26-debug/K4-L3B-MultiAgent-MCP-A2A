"""Authenticated upload to the public Competition Workspace V2 API."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx2

from .config import Settings


def submit_artifact(settings: Settings, path: Path) -> dict[str, Any]:
    with httpx2.Client(timeout=60, follow_redirects=False) as client, path.open("rb") as handle:
        response = client.post(
            f"{settings.competition_api_url}/api/v2/submissions",
            headers={"Authorization": f"Bearer {settings.team_api_key}"},
            files={"file": (path.name, handle, "application/zip")},
        )
    if not response.is_success:
        raise RuntimeError(f"Submission upload rejected (HTTP {response.status_code})")
    result = response.json()
    if not isinstance(result, dict) or not isinstance(result.get("receipt"), str):
        raise ValueError(
            "Submission API did not return a receipt; check team history before retrying"
        )
    return result


def submission_status(settings: Settings, receipt: str) -> dict[str, Any]:
    with httpx2.Client(timeout=30, follow_redirects=False) as client:
        response = client.get(
            f"{settings.competition_api_url}/api/v2/submissions/{quote(receipt, safe='')}",
            headers={"Authorization": f"Bearer {settings.team_api_key}"},
        )
    if not response.is_success:
        raise RuntimeError(f"Submission status unavailable (HTTP {response.status_code})")
    result = response.json()
    if not isinstance(result, dict):
        raise ValueError("Submission status must be an object")
    return result
