# Security policy

Please report security problems privately, through this repository's **Security → Report a vulnerability** form on GitHub. Don't open a public issue.

Useful details: what an attacker could do, how to reproduce it, and the integration and Home Assistant versions.

If your TURN key's API token may have leaked, delete the key in the Cloudflare dashboard (Realtime → TURN). That stops all credentials made with it. Then create a new key, and enter it in Home Assistant with **Reconfigure**.
