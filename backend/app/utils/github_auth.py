"""Shared GitHub repository access-verification helper.

Used by both /api/analyze and /api/actuate to confirm that the caller's
token has write access (push, maintain, or admin) to the target repository
before running any agent pipeline or mutating the repo.
"""

import logging

import httpx
from fastapi import HTTPException

logger = logging.getLogger("shipmate.github_auth")


async def verify_repo_write_access(token: str, owner: str, repo: str) -> None:
    """Confirm the token grants write access to owner/repo.

    Calls GET /repos/{owner}/{repo} and inspects the permissions object
    returned by the GitHub API. Raises an appropriate HTTPException on any
    access-control or network failure so callers need no extra error handling.
    """
    url = f"https://api.github.com/repos/{owner}/{repo}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, headers=headers)
    except httpx.RequestError as exc:
        logger.warning("GitHub repo permission check network error: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="Unable to reach GitHub API to verify repository access.",
        ) from exc

    if response.status_code == 404:
        raise HTTPException(
            status_code=404,
            detail=f"Repository '{owner}/{repo}' not found or not accessible with the provided token.",
        )

    if response.status_code == 401:
        raise HTTPException(status_code=401, detail="GitHub token is invalid or expired.")

    if response.status_code == 403:
        raise HTTPException(
            status_code=403,
            detail=f"Access denied to repository {owner}/{repo}.",
        )

    if response.status_code not in (200, 301):
        logger.warning(
            "GitHub repo permission check returned %s for %s/%s",
            response.status_code, owner, repo,
        )
        raise HTTPException(
            status_code=502,
            detail=f"Unexpected GitHub API response ({response.status_code}) while verifying repository access.",
        )

    permissions: dict = response.json().get("permissions", {})
    has_write = (
        permissions.get("admin") is True
        or permissions.get("maintain") is True
        or permissions.get("push") is True
    )

    if not has_write:
        logger.warning(
            "Token lacks write access to %s/%s (permissions=%s)",
            owner, repo, permissions,
        )
        raise HTTPException(
            status_code=403,
            detail=(
                f"The authenticated token does not have write access to '{owner}/{repo}'. "
                "Push, maintain, or admin permission is required."
            ),
        )
