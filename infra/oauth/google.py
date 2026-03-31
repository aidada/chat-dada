from __future__ import annotations

from authlib.integrations.starlette_client import OAuth

from web.config import settings


oauth = OAuth()

if settings.google_client_id and settings.google_client_secret:
    oauth.register(
        name="google",
        server_metadata_url=settings.google_server_metadata_url,
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        client_kwargs={"scope": "openid email profile"},
    )


def get_google_client():
    client = oauth.create_client("google")
    if client is None:
        raise RuntimeError("Google OAuth client is not configured")
    return client
