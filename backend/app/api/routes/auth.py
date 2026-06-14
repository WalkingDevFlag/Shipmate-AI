import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Query
from app.services.github_auth_service import GitHubAuthService
from app.services.github_api_service import GitHubAPIService
from app.services import session_store
from app.schemas.api_schemas import RepoSummary
from app.api.deps import resolve_access_token

logger = logging.getLogger("shipmate.auth_route")

router = APIRouter(prefix="/auth/github", tags=["github-auth"])
logger = logging.getLogger("shipmate.auth")


@router.get("/login")
async def github_login():
    """Return the GitHub OAuth authorization URL."""
    try:
        redirect_uri = os.getenv("GITHUB_REDIRECT_URI", "http://localhost:5173/github/callback")
        auth_url = GitHubAuthService.get_auth_url(redirect_uri)
        return {"auth_url": auth_url}
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/callback")
async def github_callback(code: str, state: str):
    """Exchange OAuth code for access token and return user profile."""
    try:
        token_data = await GitHubAuthService.exchange_code_for_token(code, state)
        access_token = token_data.get("access_token")
        if not access_token:
            raise HTTPException(status_code=400, detail="GitHub did not return an access token.")
        user_profile = await GitHubAuthService.get_user_profile(access_token)
        # Vault the raw token and hand the client an OPAQUE session id instead.
        # The raw token never crosses the network boundary again — the frontend
        # stores `session_id`, sends it as the bearer credential, and the auth
        # boundary resolves it back to the token server-side. We deliberately do
        # NOT echo the raw token (nor an `access_token` alias) — the migration
        # window is over; the frontend reads `session_id`.
        session_id = session_store.mint(access_token, scope=token_data.get("scope"))
        return {
            "success": True,
            "session_id": session_id,
            "token_type": token_data.get("token_type", "bearer"),
            "scope": token_data.get("scope"),
            "user": user_profile,
        }
    except HTTPException:
        # Already a clean, intentional error (e.g. the 400 above) — don't
        # re-wrap it into a 500 with a traceback.
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        # SECURITY: never return the traceback to the client. The failing call
        # stack runs through get_user_profile(access_token), so format_exc()
        # could serialize the OAuth access token into the HTTP response. Log the
        # full traceback server-side only; return a generic message.
        logger.exception("GitHub OAuth callback failed")
        raise HTTPException(status_code=500, detail="OAuth callback failed. Please try signing in again.")


@router.get("/me")
async def get_me(access_token: str = Depends(resolve_access_token)):
    """Return the authenticated user's GitHub profile."""
    try:
        user = await GitHubAuthService.get_user_profile(access_token)
        return {"authenticated": True, "user": user}
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))


@router.get("/repos")
async def get_repos(access_token: str = Depends(resolve_access_token)):
    """Return the authenticated user's repositories."""
    try:
        repos = await GitHubAPIService.get_user_repos(access_token)
        result = []
        for r in repos:
            result.append({
                "id": r.get("id"),
                "name": r.get("name"),
                "full_name": r.get("full_name"),
                "description": r.get("description"),
                "html_url": r.get("html_url"),
                "private": r.get("private", False),
                "default_branch": r.get("default_branch", "main"),
                "language": r.get("language"),
                "stargazers_count": r.get("stargazers_count", 0),
                "forks_count": r.get("forks_count", 0),
                "updated_at": r.get("updated_at"),
                "owner": {
                    "login": r.get("owner", {}).get("login", ""),
                    "avatar_url": r.get("owner", {}).get("avatar_url", ""),
                },
            })
        return {"repos": result, "count": len(result)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/repos/{owner}/{repo_name}/branches")
async def get_branches(owner: str, repo_name: str, access_token: str = Depends(resolve_access_token)):
    """Return branches for a repository."""
    try:
        branches = await GitHubAPIService.get_branches(access_token, owner, repo_name)
        formatted = [
            {
                "name": b.get("name"),
                "commit": {"sha": b.get("commit", {}).get("sha", "")[:7]},
                "protected": b.get("protected", False),
            }
            for b in branches
        ]
        return {"branches": formatted, "count": len(formatted)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/repos/{owner}/{repo_name}/pulls")
async def get_pulls(owner: str, repo_name: str, access_token: str = Depends(resolve_access_token)):
    """Return open pull requests for a repository."""
    try:
        pulls = await GitHubAPIService.get_open_pulls(access_token, owner, repo_name)
        formatted = [
            {
                "number": p.get("number"),
                "title": p.get("title"),
                "state": p.get("state"),
                "html_url": p.get("html_url"),
                "user": {"login": p.get("user", {}).get("login")},
                "created_at": p.get("created_at"),
            }
            for p in pulls
        ]
        return {"pulls": formatted, "count": len(formatted)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/logout")
async def logout(access_token: str = Depends(resolve_access_token)):
    # `access_token` here is the resolved real token (the dependency already
    # turned the session id back into it). Drop both the vault session that
    # referenced it and the in-memory token entry.
    try:
        session_store.revoke_token(access_token)
        GitHubAuthService.clear_token(access_token)
        return {"success": True, "message": "Logged out"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"success": True, "message": "Logged out"}
