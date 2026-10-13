import os

import msal

_msal_app = None

def _get_app() -> msal.ConfidentialClientApplication:
    global _msal_app
    if _msal_app is None:
        _msal_app = msal.ConfidentialClientApplication(
            client_id=os.environ["BC_CLIENT_ID"],
            client_credential=os.environ["BC_CLIENT_SECRET"],
            authority=f"https://login.microsoftonline.com/{os.environ['BC_TENANT_ID']}",
        )
    return _msal_app

_SCOPES = ["https://api.businesscentral.dynamics.com/.default"]


def get_bc_token() -> str:
    # msal keeps its own in-memory cache and returns the cached token until
    # it's near expiry, so this is safe to call on every event without
    # re-authenticating each time.
    app = _get_app()
    result = app.acquire_token_silent(scopes=_SCOPES, account=None)
    if not result:
        result = app.acquire_token_for_client(scopes=_SCOPES)
    if "access_token" not in result:
        raise RuntimeError(f"Token acquisition failed: {result.get('error_description')}")
    return result["access_token"]
