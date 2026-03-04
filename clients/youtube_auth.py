"""
VideoForge — YouTube OAuth2 authentication client.

Manages per-channel OAuth2 tokens for YouTube Data API v3.
Supports per-channel SOCKS5/HTTP proxy for API calls.

Token storage layout:
    config/oauth2/{channel_name}_token.pickle   ← one file per channel
    config/client_secrets.json                   ← shared OAuth client credentials

Proxy config (in channel_config JSON):
    "proxy": "socks5://user:pass@host:port"   ← SOCKS5 (recommended)
    "proxy": "http://user:pass@host:port"      ← HTTP proxy

Proxy usage:
    OAuth browser flow   → NO proxy (local browser, one-time manual action)
    Token refresh        → YES, via requests session with proxy
    All API calls        → YES, via httplib2 with proxy

Usage (CLI — first-time auth for a channel):
    python clients/youtube_auth.py --channel main
    python clients/youtube_auth.py --channel main --proxy socks5://user:pass@host:1080 --verify

Usage (import):
    from clients.youtube_auth import get_youtube_service
    service = get_youtube_service("main", proxy_url="socks5://...")
"""

from __future__ import annotations

import argparse
import logging
import pickle
import sys
from pathlib import Path
from urllib.parse import urlparse

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

log = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]

ROOT = Path(__file__).parent.parent
DEFAULT_SECRETS = ROOT / "config" / "client_secrets.json"
OAUTH2_DIR = ROOT / "config" / "oauth2"

API_SERVICE_NAME = "youtube"
API_VERSION = "v3"


# ─── Token helpers ────────────────────────────────────────────────────────────

def _token_path(channel_name: str) -> Path:
    """Return path to the channel's token pickle file."""
    OAUTH2_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = channel_name.replace(" ", "_").replace("/", "_")
    return OAUTH2_DIR / f"{safe_name}_token.pickle"


def _load_credentials(channel_name: str) -> Credentials | None:
    """Load stored credentials. Returns None if not found or invalid."""
    path = _token_path(channel_name)
    if not path.exists():
        return None
    with open(path, "rb") as f:
        creds: Credentials = pickle.load(f)
    return creds  # May be expired — caller handles refresh


def _save_credentials(channel_name: str, creds: Credentials) -> None:
    """Persist credentials to disk."""
    path = _token_path(channel_name)
    with open(path, "wb") as f:
        pickle.dump(creds, f)
    log.debug("Token saved: %s", path)


# ─── Proxy helpers ────────────────────────────────────────────────────────────

def _make_proxy_http(proxy_url: str) -> object:
    """
    Build an httplib2.Http object configured with the given proxy.

    Supports:
        socks5://user:pass@host:port   — SOCKS5 (recommended, port 64695)
        http://user:pass@host:port     — HTTP proxy (port 64694)

    Returns:
        httplib2.Http instance with proxy configured.
    """
    import httplib2

    p = urlparse(proxy_url)
    host = p.hostname
    port = p.port
    user = p.username or None
    pwd  = p.password or None

    scheme = p.scheme.lower()
    if scheme in ("socks5", "socks5h"):
        proxy_type = httplib2.socks.PROXY_TYPE_SOCKS5
    elif scheme in ("socks4", "socks4a"):
        proxy_type = httplib2.socks.PROXY_TYPE_SOCKS4
    else:
        proxy_type = httplib2.socks.PROXY_TYPE_HTTP

    proxy_info = httplib2.ProxyInfo(
        proxy_type=proxy_type,
        proxy_host=host,
        proxy_port=port,
        proxy_user=user,
        proxy_pass=pwd,
        proxy_rdns=True,   # resolve DNS through proxy (important for SOCKS5)
    )
    log.debug("Proxy configured: %s://%s:%s", scheme, host, port)
    return httplib2.Http(proxy_info=proxy_info)


def _make_proxy_session(proxy_url: str) -> object:
    """
    Build a requests.Session with proxy configured.
    Used for token refresh (google-auth uses requests transport).
    """
    import requests
    session = requests.Session()
    session.proxies = {
        "http":  proxy_url,
        "https": proxy_url,
    }
    return session


def _refresh_credentials(creds: Credentials, proxy_url: str | None) -> Credentials:
    """
    Refresh expired credentials, optionally through a proxy.

    Args:
        creds:      Credentials object with a valid refresh_token.
        proxy_url:  SOCKS5/HTTP proxy URL, or None for direct connection.
    """
    if proxy_url:
        from google.auth.transport.requests import Request as GoogleRequest
        session = _make_proxy_session(proxy_url)
        creds.refresh(GoogleRequest(session=session))
        log.debug("Token refreshed via proxy: %s", proxy_url.split("@")[-1])
    else:
        from google.auth.transport.requests import Request
        creds.refresh(Request())
        log.debug("Token refreshed (direct)")
    return creds


# ─── OAuth browser flow ───────────────────────────────────────────────────────

def _run_oauth_flow(secrets_path: Path) -> Credentials:
    """
    Run interactive OAuth2 flow (opens local browser — NO proxy).
    One-time manual action; token is saved for all future API calls.
    """
    flow = InstalledAppFlow.from_client_secrets_file(str(secrets_path), SCOPES)
    creds: Credentials = flow.run_local_server(port=0, open_browser=True)
    return creds


# ─── Public API ───────────────────────────────────────────────────────────────

def get_youtube_service(
    channel_name: str,
    secrets_path: Path | str | None = None,
    proxy_url:    str | None = None,
) -> object:
    """
    Return an authenticated YouTube Data API v3 service for the given channel.

    Token handling:
      - Valid token → used directly (refresh if expired)
      - No token    → OAuth browser flow (local, no proxy)
      - Expired     → silent refresh through proxy (if configured)

    All API calls go through proxy_url if provided.

    Args:
        channel_name:  Logical channel name (e.g. "main", "philosophy").
        secrets_path:  Path to client_secrets.json. Defaults to config/client_secrets.json.
        proxy_url:     Proxy URL for API calls + token refresh.
                       Format: "socks5://user:pass@host:port"
                       Read from channel_config["proxy"] in practice.

    Returns:
        googleapiclient Resource object ready for YouTube API calls.

    Raises:
        FileNotFoundError: If client_secrets.json is missing.
        google.auth.exceptions.RefreshError: If token refresh fails.
    """
    secrets = Path(secrets_path) if secrets_path else DEFAULT_SECRETS
    if not secrets.exists():
        raise FileNotFoundError(
            f"client_secrets.json not found at {secrets}\n"
            "Download it from https://console.cloud.google.com/auth/clients "
            "and place it at config/client_secrets.json"
        )

    creds = _load_credentials(channel_name)

    if creds is None:
        # First run — browser OAuth (no proxy, local)
        log.info("No token for channel '%s' — starting OAuth flow (browser)...", channel_name)
        creds = _run_oauth_flow(secrets)
        _save_credentials(channel_name, creds)
        log.info("Token saved: %s", _token_path(channel_name))

    elif creds.expired and creds.refresh_token:
        # Silent refresh through proxy
        log.info("Refreshing expired token for channel '%s'...", channel_name)
        creds = _refresh_credentials(creds, proxy_url)
        _save_credentials(channel_name, creds)

    if not creds.valid:
        raise RuntimeError(
            f"Credentials for channel '{channel_name}' are invalid and cannot be refreshed. "
            "Run: python clients/youtube_auth.py --channel {channel_name} --revoke"
        )

    # Build service — API calls through proxy
    if proxy_url:
        import google_auth_httplib2
        http = _make_proxy_http(proxy_url)
        authorized_http = google_auth_httplib2.AuthorizedHttp(creds, http=http)
        service = build(
            API_SERVICE_NAME, API_VERSION,
            http=authorized_http,
            cache_discovery=False,
        )
    else:
        service = build(
            API_SERVICE_NAME, API_VERSION,
            credentials=creds,
            cache_discovery=False,
        )

    if proxy_url:
        log.debug("YouTube service ready [channel=%s, proxy=%s]",
                  channel_name, proxy_url.split("@")[-1])
    else:
        log.debug("YouTube service ready [channel=%s, direct]", channel_name)

    return service


def get_youtube_service_from_config(
    channel_name:   str,
    channel_config: dict,
    secrets_path:   Path | str | None = None,
) -> object:
    """
    Convenience wrapper: reads proxy from channel_config["proxy"] automatically.

    Args:
        channel_name:   Logical channel name.
        channel_config: Loaded channel config dict (may contain "proxy" key).
        secrets_path:   Optional override for client_secrets.json path.

    Returns:
        Authenticated YouTube service.
    """
    proxy_url = channel_config.get("proxy") or None
    if proxy_url:
        log.info("Using proxy for channel '%s': ...@%s",
                 channel_name, proxy_url.split("@")[-1] if "@" in proxy_url else proxy_url)
    return get_youtube_service(channel_name, secrets_path, proxy_url)


def verify_channel(
    channel_name: str,
    secrets_path: Path | None = None,
    proxy_url:    str | None = None,
) -> dict:
    """
    Authenticate and return basic channel info to verify credentials + proxy work.

    Returns:
        dict with keys: id, title, subscriberCount
    """
    service = get_youtube_service(channel_name, secrets_path, proxy_url)
    response = (
        service.channels()
        .list(part="snippet,statistics", mine=True)
        .execute()
    )
    items = response.get("items", [])
    if not items:
        return {"id": "unknown", "title": "unknown", "subscriberCount": 0}
    item = items[0]
    return {
        "id":              item["id"],
        "title":           item["snippet"]["title"],
        "subscriberCount": int(item["statistics"].get("subscriberCount", 0)),
    }


# ─── CLI ──────────────────────────────────────────────────────────────────────

def _setup_logging() -> None:
    handler = logging.StreamHandler(
        open(sys.stdout.fileno(), mode="w", encoding="utf-8", buffering=1, closefd=False)
    )
    handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])


def main() -> None:
    _setup_logging()

    parser = argparse.ArgumentParser(
        description="Authenticate a YouTube channel and store OAuth2 token.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python clients/youtube_auth.py --channel main
  python clients/youtube_auth.py --channel main --proxy socks5://user:pass@host:1080 --verify
  python clients/youtube_auth.py --channel main --revoke
""",
    )
    parser.add_argument("--channel", required=True,
                        help="Logical channel name (e.g. 'main', 'philosophy')")
    parser.add_argument("--secrets", default=str(DEFAULT_SECRETS),
                        help=f"Path to client_secrets.json (default: {DEFAULT_SECRETS})")
    parser.add_argument("--proxy", default=None,
                        help="Proxy URL for API calls (e.g. 'socks5://user:pass@host:1080')")
    parser.add_argument("--verify", action="store_true",
                        help="Call channels.list to verify credentials + proxy work")
    parser.add_argument("--revoke", action="store_true",
                        help="Delete stored token (forces re-auth next run)")
    args = parser.parse_args()

    channel   = args.channel
    secrets   = Path(args.secrets)
    proxy_url = args.proxy or None

    if args.revoke:
        path = _token_path(channel)
        if path.exists():
            path.unlink()
            log.info("Token revoked: %s", path)
        else:
            log.info("No token found for channel '%s'", channel)
        return

    log.info("Authenticating channel: %s", channel)
    log.info("Token file: %s", _token_path(channel))
    if proxy_url:
        log.info("Proxy: ...@%s", proxy_url.split("@")[-1] if "@" in proxy_url else proxy_url)

    if args.verify:
        info = verify_channel(channel, secrets, proxy_url)
        log.info("Auth OK — Channel: '%s' (id=%s, subscribers=%s)",
                 info["title"], info["id"], info["subscriberCount"])
    else:
        get_youtube_service(channel, secrets, proxy_url)
        log.info("Token saved for channel '%s'", channel)
        log.info("Next: python clients/youtube_auth.py --channel %s --verify", channel)


if __name__ == "__main__":
    main()
