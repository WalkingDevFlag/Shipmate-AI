import { useState, useCallback, useEffect } from 'react';
import { api, setAuthToken } from '../lib/api';
import type { GitHubUser } from '../types';

export interface AuthState {
  isAuthenticated: boolean;
  user: GitHubUser | null;
  accessToken: string | null;
  loading: boolean;
  error: string | null;
}

export function useGithubAuth() {
  const [state, setState] = useState<AuthState>({
    isAuthenticated: false,
    user: null,
    accessToken: null,
    loading: true,
    error: null,
  });

  useEffect(() => {
    const token = localStorage.getItem('github_access_token');
    const user = localStorage.getItem('github_user');
    if (token && user) {
      setAuthToken(token);  // OPP-001: prime the axios interceptor on reload
      setState({ isAuthenticated: true, user: JSON.parse(user), accessToken: token, loading: false, error: null });
    } else {
      setState(s => ({ ...s, loading: false }));
    }

    // Listen for OAuth popup result — only accept messages from the same origin
    const onMessage = (e: MessageEvent) => {
      if (e.origin !== window.location.origin) return;
      if (e.data?.type === 'GITHUB_AUTH_SUCCESS') {
        const { access_token, user } = e.data;
        localStorage.setItem('github_access_token', access_token);
        localStorage.setItem('github_user', JSON.stringify(user));
        setAuthToken(access_token);  // OPP-001: header instead of query param
        setState({ isAuthenticated: true, user, accessToken: access_token, loading: false, error: null });
      }
    };
    window.addEventListener('message', onMessage);
    return () => window.removeEventListener('message', onMessage);
  }, []);

  const login = useCallback(async () => {
    try {
      setState(s => ({ ...s, loading: true, error: null }));
      const authUrl = await api.getAuthUrl();
      const w = 500, h = 620;
      const left = window.screenX + (window.outerWidth - w) / 2;
      const top = window.screenY + (window.outerHeight - h) / 2;
      const popup = window.open(authUrl, 'GitHub Login', `width=${w},height=${h},left=${left},top=${top}`);
      if (!popup) throw new Error('Popup blocked — please allow popups for this site.');
      setState(s => ({ ...s, loading: false }));
    } catch (err) {
      setState(s => ({ ...s, loading: false, error: err instanceof Error ? err.message : 'Login failed' }));
    }
  }, []);

  const logout = useCallback(async () => {
    if (state.accessToken) {
      try { await api.logout(state.accessToken); } catch { /* ignore */ }
    }
    localStorage.removeItem('github_access_token');
    localStorage.removeItem('github_user');
    setState({ isAuthenticated: false, user: null, accessToken: null, loading: false, error: null });
  }, [state.accessToken]);

  return { ...state, login, logout };
}
