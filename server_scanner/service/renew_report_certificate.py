#!/usr/bin/env python3
from __future__ import annotations

import io
from pathlib import Path
import shutil
import subprocess
import tarfile


ROOT = Path('/home/ubuntu/market-pattern-scanner')
RUNTIME = Path('/run/market-report-acme')
SECRETS = Path('/etc/credstore.encrypted')


def seal(name: str, data: bytes, destination: Path):
    pending = destination.with_suffix('.pending')
    pending.unlink(missing_ok=True)
    subprocess.run(['systemd-creds', 'encrypt', '--with-key=host', '--name='+name, '-', str(pending)], input=data, check=True)
    pending.chmod(0o600)
    pending.replace(destination)


def main():
    # ACME 账户密钥和证书私钥只在 /run 内存文件系统中解锁，持久副本由系统凭据加密。
    RUNTIME.mkdir(mode=0o700, exist_ok=True)
    state = SECRETS / 'market-tls-state.cred'
    try:
        if state.exists():
            data = subprocess.check_output(['systemd-creds', 'decrypt', '--name=tls-state', str(state), '-'])
            with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                archive.extractall(RUNTIME, filter='data')
        webroot = ROOT / 'state/acme-challenges'
        webroot.mkdir(parents=True, exist_ok=True)
        subprocess.run([str(ROOT/'acme-venv/bin/certbot'), 'certonly', '--non-interactive', '--agree-tos',
                        '--register-unsafely-without-email', '--preferred-profile', 'shortlived',
                        '--webroot', '--webroot-path', str(webroot), '--ip-address', '129.226.223.91',
                        '--config-dir', str(RUNTIME/'config'), '--work-dir', str(RUNTIME/'work'),
                        '--logs-dir', str(RUNTIME/'logs')], check=True)
        certificate = RUNTIME / 'config/live/129.226.223.91'
        pem = (certificate/'fullchain.pem').read_bytes() + (certificate/'privkey.pem').read_bytes()
        seal('tls-pem', pem, SECRETS/'market-tls-pem.cred')
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w:gz') as archive:
            archive.add(RUNTIME/'config', arcname='config')
        seal('tls-state', output.getvalue(), state)
        subprocess.run(['systemctl', 'try-restart', 'market-pattern-viewer.service'], check=True)
        print('TLS_CERTIFICATE_ENCRYPTED_AND_READY')
    finally:
        shutil.rmtree(RUNTIME, ignore_errors=True)


if __name__ == '__main__':
    main()
