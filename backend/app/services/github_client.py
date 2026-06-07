"""Shared GitHub API client with authentication.

Provides a single authenticated HTTP client for all GitHub service modules,
eliminating duplicated token-injection logic and base-URL configuration.
"""

import os
from typing import Optional

import httpx


class GitHubClient:
    """Authenticated HTTP client for GitHub API requests.
    
    Centralizes token injection, base URL configuration, and common headers
    to eliminate duplication across github_api_service.py, github_auth_service.py,
    github_pr_service.py, and other GitHub service modules.
    """
    
    def __init__(
        self,
        token: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = 30.0,
    ):
        """Initialize the GitHub API client.
        
        Args:
            token: GitHub personal access token or OAuth token. If not provided,
                   reads from GITHUB_TOKEN environment variable.
            base_url: Base URL for GitHub API. Defaults to https://api.github.com.
                      Can be overridden for GitHub Enterprise or testing.
            timeout: Request timeout in seconds. Defaults to 30.
        """
        self.token = token or os.getenv("GITHUB_TOKEN", "")
        self.base_url = (base_url or os.getenv("GITHUB_API_URL", "")).rstrip("/") or "https://api.github.com"
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None
    
    async def __aenter__(self) -> "GitHubClient":
        """Async context manager entry."""
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
            headers=self._build_headers(),
        )
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit; closes the underlying client."""
        if self._client:
            await self._client.aclose()
            self._client = None
    
    def _build_headers(self) -> dict[str, str]:
        """Build standard GitHub API headers with authentication.
        
        Returns:
            Dictionary of headers including Authorization if token is set.
        """
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "ShipMate-AI/2.0",
        }
        if self.token:
            headers["Authorization"] = f"token {self.token}"
        return headers
    
    def get_client(self) -> httpx.AsyncClient:
        """Get the underlying httpx.AsyncClient.
        
        Raises:
            RuntimeError: If called outside of async context manager.
        
        Returns:
            The configured AsyncClient instance.
        """
        if self._client is None:
            raise RuntimeError(
                "GitHubClient must be used as an async context manager. "
                "Use: async with GitHubClient() as client: ..."
            )
        return self._client
    
    async def get(
        self,
        path: str,
        params: Optional[dict] = None,
        **kwargs,
    ) -> httpx.Response:
        """Make a GET request to the GitHub API.
        
        Args:
            path: API endpoint path (e.g., "/repos/owner/repo").
            params: Query parameters.
            **kwargs: Additional arguments passed to httpx.AsyncClient.get().
        
        Returns:
            httpx.Response object.
        """
        client = self.get_client()
        return await client.get(path, params=params, **kwargs)
    
    async def post(
        self,
        path: str,
        json: Optional[dict] = None,
        **kwargs,
    ) -> httpx.Response:
        """Make a POST request to the GitHub API.
        
        Args:
            path: API endpoint path.
            json: JSON body to send.
            **kwargs: Additional arguments passed to httpx.AsyncClient.post().
        
        Returns:
            httpx.Response object.
        """
        client = self.get_client()
        return await client.post(path, json=json, **kwargs)
    
    async def patch(
        self,
        path: str,
        json: Optional[dict] = None,
        **kwargs,
    ) -> httpx.Response:
        """Make a PATCH request to the GitHub API.
        
        Args:
            path: API endpoint path.
            json: JSON body to send.
            **kwargs: Additional arguments passed to httpx.AsyncClient.patch().
        
        Returns:
            httpx.Response object.
        """
        client = self.get_client()
        return await client.patch(path, json=json, **kwargs)
    
    async def put(
        self,
        path: str,
        json: Optional[dict] = None,
        **kwargs,
    ) -> httpx.Response:
        """Make a PUT request to the GitHub API.
        
        Args:
            path: API endpoint path.
            json: JSON body to send.
            **kwargs: Additional arguments passed to httpx.AsyncClient.put().
        
        Returns:
            httpx.Response object.
        """
        client = self.get_client()
        return await client.put(path, json=json, **kwargs)
    
    async def delete(
        self,
        path: str,
        **kwargs,
    ) -> httpx.Response:
        """Make a DELETE request to the GitHub API.
        
        Args:
            path: API endpoint path.
            **kwargs: Additional arguments passed to httpx.AsyncClient.delete().
        
        Returns:
            httpx.Response object.
        """
        client = self.get_client()
        return await client.delete(path, **kwargs)
