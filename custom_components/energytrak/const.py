"""Constants for the EnergyTrak integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "energytrak"

# Config entry data keys
CONF_EMAIL: Final = "email"
CONF_MAGIC_LINK: Final = "magic_link"
CONF_API_KEY: Final = "api_key"
CONF_REFRESH_TOKEN: Final = "refresh_token"
CONF_SITE_IDS: Final = "site_ids"
CONF_SITE_NAMES: Final = "site_names"
CONF_MANUAL_SITE_IDS: Final = "manual_site_ids"

# Options
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_STALE_MINUTES: Final = "stale_minutes"

DEFAULT_SCAN_INTERVAL: Final = 30
DEFAULT_STALE_MINUTES: Final = 15

# Google / Firebase endpoints. EnergyTrak is a Firebase app: authentication
# runs through IdentityToolkit email-link sign-in and the device telemetry
# lives in Firestore documents that the signed-in user can read directly.
IDENTITY_TOOLKIT_SIGNIN: Final = (
    "https://identitytoolkit.googleapis.com/v1/accounts:signInWithEmailLink"
)
SECURE_TOKEN_REFRESH: Final = "https://securetoken.googleapis.com/v1/token"

FIRESTORE_PROJECT: Final = "bns-coreiot-prod"
FIRESTORE_DATABASE: Final = "(default)"
FIRESTORE_BASE: Final = (
    f"https://firestore.googleapis.com/v1/projects/{FIRESTORE_PROJECT}"
    f"/databases/{FIRESTORE_DATABASE}/documents"
)

SITE_COLLECTION: Final = "site"
DEVICE_COLLECTION: Final = "device"

# Refresh the ID token this many seconds before it actually expires.
TOKEN_EXPIRY_MARGIN: Final = 60

MANUFACTURER: Final = "EnergyTrak"
