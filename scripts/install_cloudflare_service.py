"""Автозапуск уже созданного Cloudflare Tunnel на этом Mac.

python -m scripts.install_cloudflare_service [--install]
Требует var/bin/cloudflared, var/tunnel.yml и существующие credentials-file.
Не создаёт туннель, не меняет DNS и не копирует секреты в репозиторий.
"""
import argparse
import os
import plistlib
import re
import subprocess
import uuid
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    binary = root / 'var/bin/cloudflared'
    config = root / 'var/tunnel.yml'
    if not binary.is_file() or not config.is_file():
        raise SystemExit('Сначала подготовьте официальный cloudflared и var/tunnel.yml')
    match = re.search(r'^tunnel:\s*([0-9a-fA-F-]+)\s*$', config.read_text(), re.M)
    if not match:
        raise SystemExit('В конфигурации отсутствует UUID туннеля')
    tunnel_id = str(uuid.UUID(match.group(1)))
    label = 'com.samaraparts.tunnel'
    logs = root / 'var/log'
    logs.mkdir(parents=True, exist_ok=True)
    value = dict(
        Label=label,
        ProgramArguments=[str(binary), 'tunnel', '--no-autoupdate', '--config', str(config),
                          'run', '--dns-resolver-addrs', '1.1.1.1:53',
                          '--dns-resolver-addrs', '1.0.0.1:53', tunnel_id],
        WorkingDirectory=str(root), RunAtLoad=True, KeepAlive=True,
        ThrottleInterval=10, ProcessType='Background',
        StandardOutPath=str(logs / 'tunnel.log'),
        StandardErrorPath=str(logs / 'tunnel-error.log'),
    )
    generated = root / 'deploy/macos/generated' / f'{label}.plist'
    generated.parent.mkdir(parents=True, exist_ok=True)
    generated.write_bytes(plistlib.dumps(value))
    if args.install:
        target = Path.home() / 'Library/LaunchAgents' / generated.name
        if target.exists() and target.read_bytes() != generated.read_bytes():
            raise SystemExit(f'Существующий LaunchAgent отличается: {target}; сначала проверьте его')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(generated.read_bytes())
        domain = f'gui/{os.getuid()}'
        if subprocess.run(['launchctl', 'print', f'{domain}/{label}'], capture_output=True).returncode:
            subprocess.run(['launchctl', 'bootstrap', domain, str(target)], check=True)
    print(('INSTALLED ' if args.install else 'PREPARED ') + label)


if __name__ == '__main__':
    main()
