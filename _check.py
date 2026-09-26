"""Quick status check via paramiko (uses SSH key)."""
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

for label, cmd in [
    ("docker ps", "docker ps --format 'table {{.Names}}\t{{.Status}}'"),
    ("compose ps", "cd /root/bot && docker compose ps 2>&1"),
    ("logs --tail 30", "cd /root/bot && docker compose logs --tail 30 2>&1"),
    ("panel internal", "curl -s -o /dev/null -w 'HTTP %{http_code}\\n' http://localhost:8080/login"),
    ("system endpoint", "curl -s -o /dev/null -w 'HTTP %{http_code}\\n' http://localhost:8080/api/system"),
]:
    print(f"\n=== {label} ===")
    _i, stdout, stderr = client.exec_command(cmd, get_pty=False)
    print(stdout.read().decode("utf-8", errors="replace"))
    err = stderr.read().decode("utf-8", errors="replace").strip()
    if err:
        print(f"[stderr] {err}")

client.close()
