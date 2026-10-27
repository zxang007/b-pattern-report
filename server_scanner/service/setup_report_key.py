#!/usr/bin/env python3
from __future__ import annotations

import base64
import getpass
import hashlib
import json
import os
import subprocess


def main():
    # 密码只通过终端无回显读取，派生密钥通过标准输入交给系统凭据存储。
    password = getpass.getpass('设置报告密码（不回显）: ')
    if len(password) < 6:
        raise SystemExit('报告密码至少六个字符。')
    salt = os.urandom(16)
    key = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 210000, 32)
    del password
    material = json.dumps({'salt': base64.b64encode(salt).decode(), 'key': base64.b64encode(key).decode()}).encode()
    subprocess.run(['sudo', '-n', 'install', '-d', '-m', '700', '/etc/credstore.encrypted'], check=True)
    subprocess.run(['sudo', '-n', 'systemd-creds', 'encrypt', '--with-key=host', '--name=report-key', '-', '/etc/credstore.encrypted/market-report.key'], input=material, check=True)
    print('报告加密凭据已保存；未持久化明文密码或派生密钥。')


if __name__ == '__main__':
    main()
