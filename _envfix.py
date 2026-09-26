"""Append MESSAGE_CONTENT_INTENT=true to the remote .env (idempotent). No restart."""
import sys
from pathlib import Path
import paramiko

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(
    "185.233.107.234",
    username="root",
    pkey=paramiko.Ed25519Key.from_private_key_file(str(Path.home() / ".ssh" / "id_ed25519")),
    timeout=15, allow_agent=False, look_for_keys=False,
)
cmd = (
    "grep -q '^MESSAGE_CONTENT_INTENT=' /root/bot/.env "
    "|| printf '\\nMESSAGE_CONTENT_INTENT=true\\n' >> /root/bot/.env; "
    "grep MESSAGE_CONTENT_INTENT /root/bot/.env"
)
_i, stdout, stderr = client.exec_command(cmd, get_pty=False)
print("Remote .env:", stdout.read().decode("utf-8", errors="replace").strip())
err = stderr.read().decode("utf-8", errors="replace").strip()
if err:
    print("[stderr]", err)
client.close()
print("DONE")
