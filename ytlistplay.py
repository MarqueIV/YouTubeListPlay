#!/usr/bin/env python3
"""YouTube Playlist Manager CLI - List and manage your YouTube playlists."""

import argparse
import http.server
import json
import os
import sys
import threading
import urllib.parse
import webbrowser

import requests

YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
OAUTH_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = "https://www.googleapis.com/auth/youtube"
REDIRECT_PORT = 8914
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}"
TOKEN_FILE = os.path.expanduser("~/.ytlistplay_tokens.json")


# --- Client secrets file ---


def load_client_secrets(path: str) -> tuple[str, str]:
    """Load client_id and client_secret from a Google Cloud Console JSON file."""
    with open(path) as f:
        data = json.load(f)
    # The JSON nests credentials under "installed" or "web"
    for key in ("installed", "web"):
        if key in data:
            creds = data[key]
            client_id = creds.get("client_id")
            client_secret = creds.get("client_secret")
            if client_id and client_secret:
                return client_id, client_secret
    print(
        f"Error: Could not find client_id/client_secret in {path}.\n"
        "Expected a Google Cloud Console OAuth client secrets JSON file.",
        file=sys.stderr,
    )
    sys.exit(1)


# --- Token persistence ---


def save_tokens(tokens: dict):
    with open(TOKEN_FILE, "w") as f:
        json.dump(tokens, f)


def load_tokens() -> dict | None:
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            return json.load(f)
    return None


# --- OAuth 2.0 flow ---


def oauth_authorize(client_id: str, client_secret: str) -> dict:
    """Run the OAuth 2.0 authorization code flow with a local redirect server."""

    auth_code_result = {}
    server_ready = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(query)
            if "code" in params:
                auth_code_result["code"] = params["code"][0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"<h2>Authorization successful!</h2>"
                    b"<p>You can close this tab and return to the terminal.</p>"
                )
            elif "error" in params:
                auth_code_result["error"] = params["error"][0]
                self.send_response(400)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    f"<h2>Authorization failed: {params['error'][0]}</h2>".encode()
                )
            else:
                self.send_response(400)
                self.end_headers()

        def log_message(self, format, *args):
            pass  # Silence request logs

    server = http.server.HTTPServer(("localhost", REDIRECT_PORT), Handler)
    server.timeout = 120

    def serve():
        server_ready.set()
        server.handle_request()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    server_ready.wait()

    auth_params = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": SCOPES,
            "access_type": "offline",
            "prompt": "consent",
        }
    )
    auth_url = f"{OAUTH_AUTH_URL}?{auth_params}"

    print(f"\nOpening browser for authorization...\n")
    print(f"If the browser doesn't open, visit this URL:\n{auth_url}\n")

    try:
        webbrowser.open(auth_url)
    except Exception:
        pass  # URL is printed above as fallback

    print("Waiting for authorization (timeout: 2 minutes)...")
    thread.join(timeout=120)
    server.server_close()

    if "error" in auth_code_result:
        print(f"Authorization failed: {auth_code_result['error']}", file=sys.stderr)
        sys.exit(1)
    if "code" not in auth_code_result:
        print("Authorization timed out.", file=sys.stderr)
        sys.exit(1)

    # Exchange auth code for tokens
    resp = requests.post(
        OAUTH_TOKEN_URL,
        data={
            "code": auth_code_result["code"],
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
        },
    )
    if resp.status_code != 200:
        print(f"Token exchange failed: {resp.text}", file=sys.stderr)
        sys.exit(1)

    tokens = resp.json()
    tokens["client_id"] = client_id
    tokens["client_secret"] = client_secret
    save_tokens(tokens)
    print("Authorization successful! Tokens saved.\n")
    return tokens


def refresh_access_token(tokens: dict) -> dict:
    """Refresh an expired access token."""
    resp = requests.post(
        OAUTH_TOKEN_URL,
        data={
            "refresh_token": tokens["refresh_token"],
            "client_id": tokens["client_id"],
            "client_secret": tokens["client_secret"],
            "grant_type": "refresh_token",
        },
    )
    if resp.status_code != 200:
        print(f"Token refresh failed: {resp.text}", file=sys.stderr)
        sys.exit(1)
    new_tokens = resp.json()
    tokens["access_token"] = new_tokens["access_token"]
    save_tokens(tokens)
    return tokens


def get_access_token(client_id: str, client_secret: str) -> str:
    """Get a valid access token, refreshing or re-authorizing as needed."""
    tokens = load_tokens()
    if tokens and "refresh_token" in tokens:
        # Try refreshing
        tokens = refresh_access_token(tokens)
        return tokens["access_token"]
    # No saved tokens — run full auth flow
    tokens = oauth_authorize(client_id, client_secret)
    return tokens["access_token"]


# --- YouTube API calls ---


def fetch_my_playlists(access_token: str) -> list[dict]:
    """Fetch all playlists for the authenticated user (handles pagination)."""
    playlists = []
    params = {
        "part": "snippet,contentDetails",
        "mine": "true",
        "maxResults": 50,
    }
    headers = {"Authorization": f"Bearer {access_token}"}

    while True:
        resp = requests.get(
            f"{YOUTUBE_API_BASE}/playlists", params=params, headers=headers
        )
        if resp.status_code == 401:
            print("Access token expired or invalid.", file=sys.stderr)
            sys.exit(1)
        if resp.status_code != 200:
            print(f"API error {resp.status_code}: {resp.text}", file=sys.stderr)
            sys.exit(1)

        data = resp.json()
        for item in data.get("items", []):
            playlists.append(
                {
                    "id": item["id"],
                    "title": item["snippet"]["title"],
                    "video_count": item["contentDetails"]["itemCount"],
                    "description": item["snippet"].get("description", ""),
                    "privacy": item["status"]["privacyStatus"]
                    if "status" in item
                    else "unknown",
                }
            )

        next_page = data.get("nextPageToken")
        if not next_page:
            break
        params["pageToken"] = next_page

    return playlists


def fetch_channel_playlists(api_key: str, channel_id: str) -> list[dict]:
    """Fetch public playlists for a channel using an API key."""
    playlists = []
    params = {
        "part": "snippet,contentDetails",
        "channelId": channel_id,
        "maxResults": 50,
        "key": api_key,
    }

    while True:
        resp = requests.get(f"{YOUTUBE_API_BASE}/playlists", params=params)
        if resp.status_code != 200:
            print(f"API error {resp.status_code}: {resp.text}", file=sys.stderr)
            sys.exit(1)

        data = resp.json()
        for item in data.get("items", []):
            playlists.append(
                {
                    "id": item["id"],
                    "title": item["snippet"]["title"],
                    "video_count": item["contentDetails"]["itemCount"],
                    "description": item["snippet"].get("description", ""),
                }
            )

        next_page = data.get("nextPageToken")
        if not next_page:
            break
        params["pageToken"] = next_page

    return playlists


# --- Display ---


def display_playlists(playlists: list[dict]):
    if not playlists:
        print("No playlists found.")
        return

    # Calculate column widths
    max_title = max(len(p["title"]) for p in playlists)
    title_width = min(max(max_title, 5), 60)

    print(f"\n{'#':<4} {'Title':<{title_width}}  {'Videos':>6}")
    print(f"{'─' * 4} {'─' * title_width}  {'─' * 6}")

    for i, p in enumerate(playlists, 1):
        title = p["title"]
        if len(title) > title_width:
            title = title[: title_width - 1] + "…"
        print(f"{i:<4} {title:<{title_width}}  {p['video_count']:>6}")

    print(f"\nTotal: {len(playlists)} playlists, {sum(p['video_count'] for p in playlists)} videos")


# --- CLI ---


def resolve_client_credentials(args) -> tuple[str, str] | None:
    """Resolve client_id and client_secret from args, secrets file, or env vars."""
    secrets_file = getattr(args, "client_secrets_file", None) or os.environ.get("YT_CLIENT_SECRETS_FILE")
    if secrets_file:
        return load_client_secrets(secrets_file)
    client_id = args.client_id or os.environ.get("YT_CLIENT_ID")
    client_secret = args.client_secret or os.environ.get("YT_CLIENT_SECRET")
    if client_id and client_secret:
        return client_id, client_secret
    return None


def cmd_auth(args):
    """Authorize with YouTube via OAuth 2.0."""
    creds = resolve_client_credentials(args)
    if not creds:
        print(
            "Error: Provide one of:\n"
            "  --client-secrets-file PATH   (Google Cloud Console JSON file)\n"
            "  --client-id ID --client-secret SECRET\n"
            "(or set YT_CLIENT_SECRETS_FILE, or YT_CLIENT_ID and YT_CLIENT_SECRET).",
            file=sys.stderr,
        )
        sys.exit(1)
    oauth_authorize(*creds)


def cmd_list(args):
    """List playlists."""
    api_key = args.api_key or os.environ.get("YT_API_KEY")
    channel_id = args.channel_id or os.environ.get("YT_CHANNEL_ID")

    if api_key and channel_id:
        playlists = fetch_channel_playlists(api_key, channel_id)
    else:
        creds = resolve_client_credentials(args)
        if creds:
            access_token = get_access_token(*creds)
            playlists = fetch_my_playlists(access_token)
        else:
            # Try saved tokens
            tokens = load_tokens()
            if tokens and "access_token" in tokens and "client_id" in tokens:
                tokens = refresh_access_token(tokens)
                playlists = fetch_my_playlists(tokens["access_token"])
            else:
                print(
                    "Error: Provide one of:\n"
                    "  --api-key KEY --channel-id ID        (for public playlists)\n"
                    "  --client-secrets-file PATH           (Google Cloud Console JSON file)\n"
                    "  --client-id ID --client-secret SECRET (for your playlists via OAuth)\n"
                    "  Or run 'auth' first to save OAuth tokens.\n",
                    file=sys.stderr,
                )
                sys.exit(1)

    display_playlists(playlists)


def main():
    parser = argparse.ArgumentParser(
        prog="ytlistplay",
        description="YouTube Playlist Manager CLI",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # --- auth command ---
    auth_parser = subparsers.add_parser("auth", help="Authorize with YouTube via OAuth 2.0")
    auth_parser.add_argument("--client-secrets-file", help="Path to Google Cloud Console OAuth client secrets JSON file")
    auth_parser.add_argument("--client-id", help="OAuth client ID")
    auth_parser.add_argument("--client-secret", help="OAuth client secret")
    auth_parser.set_defaults(func=cmd_auth)

    # --- list command ---
    list_parser = subparsers.add_parser("list", help="List your playlists")
    list_parser.add_argument("--api-key", help="YouTube Data API key")
    list_parser.add_argument("--channel-id", help="YouTube channel ID")
    list_parser.add_argument("--client-secrets-file", help="Path to Google Cloud Console OAuth client secrets JSON file")
    list_parser.add_argument("--client-id", help="OAuth client ID")
    list_parser.add_argument("--client-secret", help="OAuth client secret")
    list_parser.set_defaults(func=cmd_list)

    # --- logout command ---
    def cmd_logout(_args):
        if os.path.exists(TOKEN_FILE):
            os.remove(TOKEN_FILE)
            print("Saved tokens removed.")
        else:
            print("No saved tokens found.")

    logout_parser = subparsers.add_parser("logout", help="Remove saved OAuth tokens")
    logout_parser.set_defaults(func=cmd_logout)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
