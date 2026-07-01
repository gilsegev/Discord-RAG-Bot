# Admin Access

## Purpose
This document explains how trusted project maintainers can access the Oracle server with admin-level SSH access.

This is different from limited regression access. Admin access uses the shared `ubuntu` server account and allows maintainers to inspect the repo, manage Docker services, run commands, and open tunnels to local services.

## Who Has Admin Access
The following public keys are installed on the Oracle server under the `ubuntu` account:

- Haragonda: `haragonda-altctrldeliver-eval`
- AltCtrlDeliver: `shilpi-tpm-rag-eval`

Each maintainer should use their own private key that matches the public key they provided.

## Log In To The Server
Use:

```bash
ssh -i /path/to/private/key ubuntu@discord-notifier.duckdns.org
```

Example:

```bash
ssh -i ~/.ssh/n8n-oracle.key ubuntu@discord-notifier.duckdns.org
```

## Open n8n And Phoenix Tunnels
Use this when you need browser access to n8n and Phoenix from your own machine:

```bash
ssh -i /path/to/private/key \
  -L 5679:127.0.0.1:5679 \
  -L 6006:127.0.0.1:6006 \
  ubuntu@discord-notifier.duckdns.org
```

Keep this terminal open while using the tunneled services.

Then open:

```text
http://127.0.0.1:5679
```

for n8n, and:

```text
http://127.0.0.1:6006
```

for Phoenix.

## Important Security Notes
- Do not share private keys.
- Do not commit private keys, API keys, Discord webhooks, or `.env` files to Git.
- This access is admin-level server access, not limited evaluator access.
- Use the evaluator-specific access docs for tunnel-only regression access:
  - `docs/AltCtrlDeliver regression access.md`
  - `docs/Haragonda regression access.md`

## Revoking Access
To revoke admin access, remove the maintainer's public key from:

```text
/home/ubuntu/.ssh/authorized_keys
```

Then verify permissions:

```bash
chmod 700 /home/ubuntu/.ssh
chmod 600 /home/ubuntu/.ssh/authorized_keys
```
