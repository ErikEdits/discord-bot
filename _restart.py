"""Bring container back up and verify. SSH key auth, UTF-8 safe."""
import sys
import time
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

print("==> docker compose up -d --build (builds with psutil, ~2-3 min)")
_i, stdout, stderr = client.exec_command(
    "cd /root/bot && docker compose up -d --build 2>&1",
    get_pty=False, timeout=600,
)
# Read output safely
for raw_line in iter(stdout.readline, ""):
    if not raw_line:
        break
    print(raw_line.rstrip().encode("utf-8", errors="replace").decode("utf-8"))

print("\n==> Waiting 15s for boot...")
time.sleep(15)

print("\n==> docker compose ps:")
_i, stdout, _e = client.exec_command("cd /root/bot && docker compose ps", get_pty=False)
print(stdout.read().decode("utf-8", errors="replace"))

print("==> Last 30 log lines:")
_i, stdout, _e = client.exec_command("cd /root/bot && docker compose logs --tail 30", get_pty=False)
print(stdout.read().decode("utf-8", errors="replace"))

print("==> Panel test:")
_i, stdout, _e = client.exec_command("curl -s -o /dev/null -w 'HTTP %{http_code}\\n' http://localhost:8080/login", get_pty=False)
print("   /login -> " + stdout.read().decode("utf-8", errors="replace").strip())

_i, stdout, _e = client.exec_command("curl -s -o /dev/null -w 'HTTP %{http_code}\\n' http://localhost:8080/api/system", get_pty=False)
print("   /api/system -> " + stdout.read().decode("utf-8", errors="replace").strip() + " (401 = auth required, endpoint exists)")

client.close()
print("\n==> DONE")
