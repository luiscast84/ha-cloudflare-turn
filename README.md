# Cloudflare TURN for Home Assistant

[![Marketplace and HACS: custom repository](https://img.shields.io/badge/Marketplace%20%7C%20HACS-custom%20repository-41BDF5.svg)](https://www.home-assistant.io/integrations/marketplace)
[![Validate](https://github.com/luiscast84/ha-cloudflare-turn/actions/workflows/validate.yml/badge.svg)](https://github.com/luiscast84/ha-cloudflare-turn/actions/workflows/validate.yml)
[![Tests](https://github.com/luiscast84/ha-cloudflare-turn/actions/workflows/tests.yml/badge.svg)](https://github.com/luiscast84/ha-cloudflare-turn/actions/workflows/tests.yml)

Relays Home Assistant's WebRTC camera streams through [Cloudflare's TURN service](https://developers.cloudflare.com/realtime/turn/) when no direct connection is possible. The short-lived TURN credentials renew themselves, entirely inside Home Assistant.

Without a relay, WebRTC needs a direct path between your phone and Home Assistant. That path often doesn't exist on mobile data, behind carrier-grade NAT, on a corporate or full-tunnel VPN, or on strict guest Wi-Fi. The live view then falls back to slower HLS, and features that only work over WebRTC stop working, such as talking through a camera (two-way audio).

## How it works

- You create a TURN key in the Cloudflare dashboard and enter it once in Home Assistant. The key stays in Home Assistant.
- The integration asks Cloudflare for credentials that are valid for 48 hours (Cloudflare's maximum), and renews them every 12 hours. Every new stream gets credentials with at least 36 hours left. That matters because Cloudflare cuts a relayed session shortly after its credentials expire.
- It registers them through Home Assistant's official ICE server registry, the same mechanism Home Assistant Cloud uses. Home Assistant hands them to every new WebRTC session: to the browser and the mobile apps, and to go2rtc for the camera side.
- If Cloudflare can't be reached, it keeps the current credentials and retries after 1, 2, 4… minutes, up to every 30 minutes. A Cloudflare outage of up to 36 hours goes unnoticed.
- If refreshing keeps failing, it says so in **Settings → Repairs**: first as a warning, when less than 24 hours are left, then as an error, when the credentials are about to expire. Credentials with less than an hour left are no longer handed out. The notice goes away by itself once a refresh succeeds.
- If Cloudflare rejects the key (deleted key, revoked token), Home Assistant asks you to re-enter it. Retrying continues meanwhile, so a temporary refusal heals by itself.
- Renewal doesn't depend on any entity, and the integration's polling setting doesn't affect it.

## Requirements

- Home Assistant 2026.1 or newer.
- A Cloudflare account. The free plan is enough.
- Cameras that stream over WebRTC in Home Assistant: cameras behind Home Assistant's built-in go2rtc, and cameras with native WebRTC.

### Cost

Cloudflare bills TURN only for data it relays to the clients, and only when the relay is actually used. The first 1,000 GB each month are free; after that it's $0.05 per GB ([Cloudflare's FAQ](https://developers.cloudflare.com/realtime/turn/faq/)). At home, most streams connect directly and don't use the relay at all.

## Installation

[![Open your Home Assistant instance and open this repository in the Marketplace or HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=luiscast84&repository=ha-cloudflare-turn&category=integration)

### Marketplace (Home Assistant 2026.11 and newer)

Home Assistant 2026.11 built HACS in as the **Marketplace**.

1. Select the button above. Or open **Marketplace**, go to **Custom repositories**, and add `https://github.com/luiscast84/ha-cloudflare-turn` with the type **Integration**. The first time, the Marketplace asks you to read and accept its warning.
2. Open **Cloudflare TURN** and select **Install**.
3. Restart Home Assistant when Repairs shows **Restart required for Cloudflare TURN**.

### HACS (Home Assistant 2026.1 to 2026.10)

1. Select the button above. Or open HACS, then **⋮ → Custom repositories**, and add `https://github.com/luiscast84/ha-cloudflare-turn` with the type **Integration**.
2. Find **Cloudflare TURN** in HACS, download it, and restart Home Assistant.

### Manually

Copy `custom_components/cloudflare_turn` into your Home Assistant `config/custom_components/` folder and restart Home Assistant.

## Setup

1. In the [Cloudflare dashboard](https://dash.cloudflare.com/?to=/:account/calls), open **Realtime → TURN** and create a TURN key.
2. Copy its **TURN Token ID** and **API token**. Cloudflare shows the API token only once.
3. In Home Assistant, go to **Settings → Devices & services → Add integration**, choose **Cloudflare TURN**, and paste both values.

Home Assistant checks the key with Cloudflare before saving it.

### Options

**Settings → Devices & services → Cloudflare TURN → Configure** chooses the relay transports:

| Transport | Use |
|---|---|
| **UDP 3478** (default) | The fastest path, wherever UDP is allowed. |
| **TLS 443** (default) | Looks like HTTPS, so it gets through almost any firewall, proxy or VPN. |
| UDP 443, TCP 3478, TCP 80, TLS 5349 | Alternatives for unusual networks. |

Every transport adds candidates that both ends have to gather and test for each stream, so keep the list short. Changes apply to the next stream without a restart. If Cloudflare stops offering all of the chosen transports, every transport it offers is used, and a warning is logged.

### Changing the key

Choose **Reconfigure** on the integration entry. To cut off access immediately (for example, if the API token leaked), delete the TURN key in the Cloudflare dashboard; that stops all credentials made with it.

## Checking that it works

- The diagnostic sensor **Credential expiry** shows when the current credentials expire. It moves forward every 12 hours.
- **⋮ → Download diagnostics** on the integration entry lists the relay URLs handed to new streams under `ice_servers_offered`. The key and the credentials are redacted.
- On your phone, turn Wi-Fi off and open a camera. The live view should start over WebRTC: features such as the talk button work only over WebRTC.

## Security and privacy

- The TURN key's API token is stored in Home Assistant's configuration, like every integration's secrets, and is only sent to Cloudflare's API.
- Apps and go2rtc only receive credentials that expire within 48 hours. Any logged-in Home Assistant user whose app or browser opens a WebRTC stream gets them, as with Home Assistant Cloud's relay.
- Media stays end-to-end encrypted (DTLS-SRTP) through the relay. Cloudflare processes only the connection metadata it needs: IP addresses, ports and timing ([Cloudflare's FAQ](https://developers.cloudflare.com/realtime/turn/faq/)).
- Only well-formed TURN URLs are passed on. go2rtc refuses a whole WebRTC session if a single ICE server is malformed, so the integration validates every URL from Cloudflare first.

## Limitations

- The credentials are shared by all streams within a 12-hour window, not issued per stream: Home Assistant asks for ICE servers synchronously, for every stream, so they must be ready in advance.
- Cloudflare issues IPv4 relay addresses only.
- A relay helps a stream connect; it doesn't fix a camera or a go2rtc problem. If a camera doesn't stream over WebRTC on your home network, it won't over the relay either.

## Removal

**Settings → Devices & services → Cloudflare TURN → ⋮ → Delete**, then remove it in HACS. Delete the TURN key in the Cloudflare dashboard as well.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy
.venv/bin/pytest
```

The tests cover every line and branch, against the latest Home Assistant and, in CI, against Home Assistant 2026.1, the oldest supported version (pinned in `.github/workflows/tests.yml`). `python script/brand.py` redraws the icons in `custom_components/cloudflare_turn/brand/`.

## Disclaimer

This is a community integration. It isn't made, endorsed or supported by Cloudflare or by the Home Assistant project. Cloudflare is a trademark of Cloudflare, Inc. The icon is original artwork.

## License

[Apache License 2.0](LICENSE)
