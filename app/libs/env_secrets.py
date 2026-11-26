"""
Read secrets from the process environment instead of the Databutton secrets API.

The app is hosted on Render, but every `db.secrets.get(...)` call still made an
HTTP request to Databutton to fetch the value. When Databutton stopped returning
the secrets, every endpoint that needs one failed with a 500.

`install()` replaces `databutton.secrets.get` with a lookup in `os.environ`, so
the existing call sites keep working unchanged and the values come from the
environment variables configured on the host (Render dashboard, or a local .env).

Databutton is only asked as a fallback, and only if a secret is missing from the
environment and the Databutton credentials are still configured. A secret that
cannot be found anywhere raises KeyError with the name of the missing variable.
"""
import os

import databutton as db

_databutton_get = db.secrets.get


def get_secret(name: str) -> str:
    value = os.environ.get(name)
    if value:
        return value

    if os.environ.get("DATABUTTON_PROJECT_ID") and os.environ.get("DATABUTTON_TOKEN"):
        try:
            return _databutton_get(name)
        except Exception as e:
            print(f"Secret {name} is not set in the environment; Databutton fallback failed: {e}")

    raise KeyError(f"Secret {name} is not configured. Set it as an environment variable.")


def install() -> None:
    """Route every db.secrets.get(...) call in the app through get_secret."""
    db.secrets.get = get_secret
