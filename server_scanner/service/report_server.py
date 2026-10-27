#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import html
import json
import os
from pathlib import Path
import re
import urllib.parse

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


GATE = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>行情扫描报告</title>
<style>body{margin:0;background:#111827;color:#e5e7eb;font:16px/1.7 system-ui}#gate{max-width:360px;margin:15vh auto;padding:24px}input,button{box-sizing:border-box;padding:12px;font:inherit;width:100%;margin:8px 0}input{background:#1f2937;color:white;border:1px solid #475569}button{cursor:pointer;background:#93c5fd;border:0}#error{color:#fca5a5}#report{max-width:1200px;margin:32px auto;padding:0 20px}#preview{position:fixed;inset:0;background:#111827;overflow:auto;padding:20px;z-index:10}#preview img{width:1600px;max-width:none}#preview button{width:auto}</style>
<form id="gate"><h1>行情扫描报告</h1><p>输入密码查看结构图和扫描结果</p><input id="password" type="password" autocomplete="current-password" placeholder="报告密码" required><button>打开报告</button><p id="error"></p></form>
<main id="report" hidden></main><section id="preview" hidden><button id="close-preview">返回报告</button><img alt="候选结构图"></section>
<script src="/report_crypto.js"></script><script>
let key=null;const decoder=new TextDecoder(),report=document.querySelector('#report');
const bytes=v=>Uint8Array.from(atob(v),c=>c.charCodeAt(0));
async function packet(path){const url=new URL(path,location.href);url.search='encrypted=1';const response=await fetch(url,{cache:'no-store'});if(!response.ok)throw Error('报告暂不可用，请稍后重试');return response.json()}
async function unpack(path){const p=await packet(path);return {data:reportCrypto.decrypt(key,bytes(p.nonce),bytes(p.ciphertext)),type:p.type}}
async function image(img){if(!img.dataset.source||img.dataset.loaded)return;img.dataset.loaded='1';try{const value=await unpack(img.dataset.source);img.src=URL.createObjectURL(new Blob([value.data],{type:value.type}))}catch{img.alt='图形读取失败，刷新后重试';delete img.dataset.loaded}}
async function show(path){const logical=path==='/'||path==='/index.html'?'/latest/index.html':path;const value=await unpack(logical);const parsed=new DOMParser().parseFromString(decoder.decode(value.data),'text/html');report.innerHTML=Array.from(parsed.head.querySelectorAll('style')).map(e=>e.outerHTML).join('')+parsed.body.innerHTML;history.replaceState(null,'',logical);for(const img of report.querySelectorAll('img')){img.dataset.source=new URL(img.getAttribute('src'),location.href).pathname;img.removeAttribute('src');if(!img.closest('details')||img.closest('details').open)image(img)}for(const detail of report.querySelectorAll('details'))detail.addEventListener('toggle',()=>{if(detail.open)detail.querySelectorAll('img').forEach(image)});report.hidden=false;document.querySelector('#gate').hidden=true}
document.querySelector('#gate').addEventListener('submit',async event=>{event.preventDefault();const error=document.querySelector('#error');error.textContent='正在解锁…';try{const p=await packet(location.pathname);key=await reportCrypto.derive(document.querySelector('#password').value,bytes(p.salt));document.querySelector('#password').value='';await show(location.pathname);error.textContent=''}catch{key=null;error.textContent='密码错误或报告暂不可用'}});
report.addEventListener('click',async event=>{const link=event.target.closest('a');if(!link)return;const url=new URL(link.getAttribute('href'),location.href);if(url.origin!==location.origin)return;event.preventDefault();try{if(url.pathname.endsWith('.html')||url.pathname==='/history'||url.pathname==='/'){await show(url.pathname);return}const value=await unpack(url.pathname);const blob=URL.createObjectURL(new Blob([value.data],{type:value.type}));if(value.type==='image/svg+xml'){document.querySelector('#preview img').src=blob;document.querySelector('#preview').hidden=false}else{const a=document.createElement('a');a.href=blob;a.download=url.pathname.split('/').pop();a.click();setTimeout(()=>URL.revokeObjectURL(blob),1000)}}catch{alert('读取失败，请稍后重试')}});
document.querySelector('#close-preview').onclick=()=>{const img=document.querySelector('#preview img');URL.revokeObjectURL(img.src);img.removeAttribute('src');document.querySelector('#preview').hidden=true};
</script></html>'''


def encrypted_packet(content: bytes, kind: str, credential: dict) -> bytes:
    nonce = os.urandom(12)
    key = base64.b64decode(credential["key"])
    return json.dumps({"salt": credential["salt"], "nonce": base64.b64encode(nonce).decode(),
                       "ciphertext": base64.b64encode(AESGCM(key).encrypt(nonce, content, None)).decode(),
                       "type": kind, "iterations": 210000, "version": 1}).encode()


def report_path(root: Path, url: str) -> Path | None:
    path = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
    if path in ("/", "/index.html"):
        path = "/latest/index.html"
    if path == "/status.json":
        return root / "status.json"
    parts = path.strip("/").split("/")
    if parts[0] == "latest":
        relative = parts[1:]
        folder = root / "latest"
    elif len(parts) > 2 and parts[0] == "runs" and re.fullmatch(r"(?:live|backtest)-[0-9a-f]{20}", parts[1]):
        relative = parts[2:]
        folder = root / "runs" / parts[1]
    else:
        return None
    # 只公开报表、候选数据和图形；数据库、输入缓存及目录列表不对外提供。
    allowed = len(relative) == 1 and relative[0] in (
        "index.html", "matches.csv", "summary.json", "events.json", "failed_symbols.json",
    )
    allowed = allowed or (len(relative) == 2 and relative[0] == "charts" and
                          re.fullmatch(r"[0-9a-f]{32}\.svg", relative[1]) is not None)
    if not allowed:
        return None
    candidate = folder.joinpath(*relative).resolve()
    if not candidate.is_relative_to((root / "runs").resolve()):
        return None
    return candidate


def handler(root: Path, credential: dict):
    class ReportHandler(BaseHTTPRequestHandler):
        def serve(self, head=False):
            path = urllib.parse.urlsplit(self.path).path
            if path == '/report_crypto.js':
                content = Path(__file__).with_name('report_crypto.js').read_bytes()
                kind = 'application/javascript'
                return self.respond(content, kind, head)
            if urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get('encrypted') != ['1']:
                return self.respond(GATE.encode(), 'text/html; charset=utf-8', head)
            if path == "/history":
                cards = []
                for summary in sorted((root / "runs").glob("*/summary.json"), key=lambda p: p.stat().st_mtime, reverse=True):
                    if not re.fullmatch(r"(?:live|backtest)-[0-9a-f]{20}", summary.parent.name):
                        continue
                    data = json.loads(summary.read_text())
                    label = f"{data.get('start_bj','')} → {data.get('end_bj_exclusive','')} · {data.get('status','')} · {data.get('matches',0)} 条"
                    cards.append(f'<p><a href="/runs/{summary.parent.name}/index.html">{html.escape(label)}</a></p>')
                content = ('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>历史扫描与回测</title><style>body{max-width:1000px;margin:32px auto;padding:0 20px;background:#111827;color:#e5e7eb;font:16px/1.8 system-ui}a{color:#93c5fd}</style><h1>历史扫描与回测</h1><p><a href="/">最新扫描</a></p>' + ''.join(cards)).encode()
                kind = "text/html; charset=utf-8"
            else:
                target = report_path(root, self.path)
                if target is None or not target.is_file():
                    self.send_error(404)
                    return
                content = target.read_bytes()
                kind = {".svg": "image/svg+xml", ".csv": "text/csv; charset=utf-8", ".json": "application/json; charset=utf-8", ".html": "text/html; charset=utf-8"}[target.suffix]
                if target.suffix == ".html":
                    navigation = '<nav><a href="/">最新扫描</a> | <a href="/history">历史扫描与回测</a> | <a href="/status.json">运行状态</a></nav>'.encode()
                    content = content.replace(b"<h1>", navigation + b"<h1>", 1)
            return self.respond(encrypted_packet(content, kind, credential), 'application/json', head)

        def respond(self, content, kind, head):
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if not head:
                self.wfile.write(content)

        def do_GET(self):
            self.serve()

        def do_HEAD(self):
            self.serve(head=True)

    return ReportHandler


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="仅公开行情报表的无密码查看服务。")
    parser.add_argument("--root", required=True)
    parser.add_argument("--port", type=int, default=80)
    parser.add_argument("--bind", default="0.0.0.0")
    args = parser.parse_args()
    credential_path = Path(os.environ['CREDENTIALS_DIRECTORY']) / 'report-key'
    credential = json.loads(credential_path.read_text())
    if len(base64.b64decode(credential['key'])) != 32:
        raise SystemExit('报表加密凭据无效。')
    ThreadingHTTPServer((args.bind, args.port), handler(Path(args.root).resolve(), credential)).serve_forever()
