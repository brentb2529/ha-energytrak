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

# Local B-Infohub bridge (optional; almost no installation has one).
# Stored in entry.data rather than options because the host and key are
# identity, not preference -- changing them means a different device.
CONF_LOCAL_HOST: Final = "local_host"
CONF_LOCAL_PORT: Final = "local_port"
CONF_LOCAL_KEY: Final = "local_encryption_key"
CONF_LOCAL_SITE_ID: Final = "local_site_id"
DEFAULT_LOCAL_PORT: Final = 6053

# A local-only installation has no EnergyTrak account and therefore no site id,
# but every device, entity and unique_id in this integration is keyed by one.
# Rather than thread "or None" through all of that, a bridge-only entry gets a
# synthetic site id derived from its host. Opaque strings all the way down, so
# nothing else needs to know the difference.
LOCAL_SITE_PREFIX: Final = "bridge"

# Which halves of the integration an entry has configured. An entry may be
# cloud-only, bridge-only, or both -- all three are first-class.
CONF_HAS_CLOUD: Final = "has_cloud"

# The bridge advertises this in its mDNS TXT record. Discovery is filtered on
# it so this integration only ever offers to adopt a B-Infohub, never somebody
# else's ESPHome doorbell.
LOCAL_PROJECT_NAME: Final = "bbensten.b_infohub"

# Options
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_STALE_MINUTES: Final = "stale_minutes"

DEFAULT_SCAN_INTERVAL: Final = 30
DEFAULT_STALE_MINUTES: Final = 15

# Storage for the observed equipment-liveness signature (see
# normalize.EquipmentFreshness). Kept out of the config entry so it can be
# discarded without touching credentials.
FRESHNESS_STORE_VERSION: Final = 1

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

# Generator artwork, served as static files so entities can point at it with
# `entity_picture` and picture cards can use it directly.
IMAGE_URL_BASE: Final = "/api/energytrak/static"
IMAGE_DIR: Final = "images"
