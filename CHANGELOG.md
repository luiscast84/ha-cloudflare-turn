# Changelog

## 0.1.0

First release.

- Hands short-lived Cloudflare TURN credentials to every new WebRTC session, through Home Assistant's ICE server registry: browsers and apps on one side, go2rtc on the other.
- Credentials last 48 hours and renew every 12 hours. Failed refreshes retry with backoff, from 1 up to 30 minutes, and honour Cloudflare's Retry-After.
- Reports long refresh failures in Repairs, and stops handing out credentials with less than an hour left.
- Reauthentication when Cloudflare rejects the key, and reconfiguration to switch keys.
- Options to choose the relay transports (UDP 3478 and TLS 443 by default).
- Diagnostic sensor with the credentials' expiry, and diagnostics with every secret redacted.
- English and Spanish.
