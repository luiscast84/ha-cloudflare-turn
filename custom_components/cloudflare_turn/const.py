"""Constants for the Cloudflare TURN integration."""

from datetime import timedelta
from typing import Final

DOMAIN: Final = "cloudflare_turn"
TITLE: Final = "Cloudflare TURN"

CONF_KEY_ID: Final = "key_id"
CONF_API_TOKEN: Final = "api_token"  # noqa: S105 (a config key, not a secret)
CONF_TRANSPORTS: Final = "transports"

API_BASE_URL: Final = "https://rtc.live.cloudflare.com/v1/turn/keys"
DASHBOARD_URL: Final = "https://dash.cloudflare.com/?to=/:account/calls"
STATUS_URL: Final = "https://www.cloudflarestatus.com/"
REQUEST_TIMEOUT: Final = 20  # seconds

# Cloudflare lets a credential live 48 hours at most. A relayed session is cut
# shortly after its credential expires, so credentials are renewed long before
# that: every new session gets one with at least 36 hours left, and a Cloudflare
# outage of up to 36 hours goes unnoticed.
CREDENTIAL_TTL: Final = timedelta(hours=48)
REFRESH_INTERVAL: Final = timedelta(hours=12)
# Credentials this close to expiry are no longer handed out.
EXPIRY_MARGIN: Final = timedelta(hours=1)
# Lifetime of the throwaway credential requested to validate a key.
VALIDATION_TTL: Final = timedelta(hours=1)

# Retries after a failed refresh: 1 minute, doubling up to 30 minutes.
RETRY_MIN: Final = timedelta(minutes=1)
RETRY_MAX: Final = timedelta(minutes=30)

ISSUE_REFRESH_FAILING: Final = "refresh_failing"
# A failing refresh is reported in Repairs once the credentials in use have
# less than this left.
ISSUE_WHEN_LEFT: Final = timedelta(hours=24)

# Relay transports Cloudflare offers, named "<protocol>_<port>". Port 53 is left
# out: browsers block it, and without trickle ICE it only delays the connection.
TRANSPORTS: Final = (
    "udp_3478",
    "udp_443",
    "tcp_3478",
    "tcp_80",
    "tls_5349",
    "tls_443",
)
# UDP 3478 is the fastest path; TLS on 443 looks like HTTPS and gets through
# almost any firewall or VPN. Each extra transport adds candidates to gather.
DEFAULT_TRANSPORTS: Final = ("udp_3478", "tls_443")
