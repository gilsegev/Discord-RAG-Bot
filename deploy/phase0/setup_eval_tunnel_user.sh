#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this script with sudo." >&2
  exit 1
fi

if [[ $# -ne 2 ]]; then
  echo "Usage: sudo $0 <linux_username> <public_key_file>" >&2
  exit 1
fi

username="$1"
public_key_file="$2"
n8n_target="127.0.0.1:5679"

if [[ ! "${username}" =~ ^n8n_eval_[a-z0-9_]+$ ]]; then
  echo "Username must start with n8n_eval_ and contain lowercase letters, digits, or underscores." >&2
  exit 1
fi

if [[ ! -f "${public_key_file}" ]]; then
  echo "Public key file not found: ${public_key_file}" >&2
  exit 1
fi

public_key="$(grep -m 1 -E '^ssh-ed25519 ' "${public_key_file}" || true)"
if [[ -z "${public_key}" ]]; then
  echo "Expected one ssh-ed25519 public key in ${public_key_file}." >&2
  exit 1
fi

if ! id "${username}" >/dev/null 2>&1; then
  adduser --disabled-password --gecos "n8n evaluation tunnel" "${username}"
fi

home_dir="$(getent passwd "${username}" | cut -d: -f6)"
ssh_dir="${home_dir}/.ssh"
authorized_keys="${ssh_dir}/authorized_keys"
sshd_fragment="/etc/ssh/sshd_config.d/90-${username}.conf"

install -d -m 700 -o "${username}" -g "${username}" "${ssh_dir}"
printf 'restrict,port-forwarding,permitopen="%s" %s\n' "${n8n_target}" "${public_key}" > "${authorized_keys}"
chown "${username}:${username}" "${authorized_keys}"
chmod 600 "${authorized_keys}"

cat > "${sshd_fragment}" <<EOF
Match User ${username}
    AuthenticationMethods publickey
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    PermitTTY no
    X11Forwarding no
    AllowAgentForwarding no
    AllowTcpForwarding local
    GatewayPorts no
    PermitOpen ${n8n_target}
    ForceCommand /usr/sbin/nologin
EOF

/usr/sbin/sshd -t
systemctl reload ssh

echo "Created restricted tunnel account: ${username}"
echo "Permitted destination: ${n8n_target}"
echo "SSH configuration: ${sshd_fragment}"
echo "Keep the current administrator session open while testing the new tunnel."
