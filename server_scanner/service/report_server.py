#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import html
import json
import os
from pathlib import Path
import re
import ssl
import urllib.parse

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class TLSHTTPServer(ThreadingHTTPServer):
    def __init__(self, address, handler_class, context):
        self.tls_context = context
        super().__init__(address, handler_class)

    def process_request_thread(self, request, client_address):
        # 每条连接独立握手，浏览器预连接不会阻塞其他报告或图形请求。
        try:
            request.settimeout(20)
            secure = self.tls_context.wrap_socket(request, server_side=True)
            super().process_request_thread(secure, client_address)
        except (OSError, TimeoutError):
            self.shutdown_request(request)


GATE = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>行情扫描报告</title>
<style>body{margin:0;background:#111827;color:#e5e7eb;font:16px/1.7 system-ui}#gate{max-width:360px;margin:15vh auto;padding:24px}input,button{box-sizing:border-box;padding:12px;font:inherit;width:100%;margin:8px 0}input{background:#1f2937;color:white;border:1px solid #475569}button{cursor:pointer;background:#93c5fd;border:0}#error{color:#fca5a5}#report{max-width:1200px;margin:32px auto;padding:0 20px}#preview{position:fixed;inset:0;background:#111827;overflow:auto;padding:20px;z-index:10}#preview img{width:1600px;max-width:none}#preview button{width:auto}.copy-symbol{width:auto;min-height:40px;margin:0 5px 0 0;padding:4px 9px;border:1px solid #506787;border-radius:5px;background:#223047;color:#bfdbfe;vertical-align:middle;font-weight:600}.copy-symbol small{font-size:.72em;opacity:.75;margin-left:3px}.copy-symbol:focus-visible{outline:2px solid #93c5fd;outline-offset:2px}#copy-status{position:fixed;bottom:max(18px,env(safe-area-inset-bottom));left:50%;transform:translateX(-50%);z-index:30;max-width:90vw;padding:10px 16px;border-radius:6px;background:#dbeafe;color:#172554;box-shadow:0 3px 14px #0006}#copy-fallback{position:fixed;inset:0;background:#0008;display:grid;place-items:center;z-index:40;padding:20px}#copy-fallback[hidden]{display:none}#copy-fallback section{width:min(360px,100%);background:#1f2937;border:1px solid #475569;padding:20px;border-radius:8px}</style>
<form id="gate"><h1>行情扫描报告</h1><p>输入密码查看结构图和扫描结果</p><input id="password" type="password" autocomplete="current-password" placeholder="报告密码" required><button>打开报告</button><p id="error"></p></form>
<button id="forget-device" hidden style="width:auto;margin:16px 20px">忘记本设备</button><main id="report" hidden></main><section id="preview" hidden><button id="close-preview">返回报告</button><img alt="候选结构图"></section><div id="copy-status" role="status" aria-live="polite" hidden></div><div id="copy-fallback" hidden><section role="dialog" aria-modal="true" aria-labelledby="copy-help"><p id="copy-help">自动复制未获允许，长按下方币名即可复制</p><input id="copy-value" readonly aria-label="待复制的币名"><button id="close-copy">关闭</button></section></div>
<script>
const decoder=new TextDecoder(),report=document.querySelector('#report'),reportSalt='__REPORT_SALT__';
const workerSource=atob('__CRYPTO_BASE64__')+`
let privateKey=null;
const bytes=v=>Uint8Array.from(atob(v),c=>c.charCodeAt(0));
self.onmessage=async event=>{const {id,action,value}=event.data;try{
if(action==='init'){privateKey=await reportCrypto.derive(value.password,bytes(value.salt));postMessage({id,result:true});return}
if(action==='restore'){privateKey=new Uint8Array(value);postMessage({id,result:true});return}
if(!privateKey)throw Error('请先解锁');
if(action==='export'){const result=privateKey.slice();postMessage({id,result},[result.buffer]);return}
const result=reportCrypto.decrypt(privateKey,bytes(value.nonce),bytes(value.ciphertext));
postMessage({id,result},[result.buffer]);
}catch{postMessage({id,error:'密码错误或报表校验失败'})}};`;
let cryptoWorker=null,sequence=0,busy=false;const pending=new Map();
function startWorker(){if(cryptoWorker)return;const source=URL.createObjectURL(new Blob([workerSource],{type:'application/javascript'}));cryptoWorker=new Worker(source);cryptoWorker.onmessage=event=>{const item=pending.get(event.data.id);if(!item)return;pending.delete(event.data.id);clearTimeout(item.timer);event.data.error?item.reject(Error(event.data.error)):item.resolve(event.data.result)};cryptoWorker.onerror=()=>{for(const item of pending.values()){clearTimeout(item.timer);item.reject(Error('浏览器解锁线程异常，请刷新重试'))}pending.clear();cryptoWorker.terminate();cryptoWorker=null};setTimeout(()=>URL.revokeObjectURL(source),1000)}
function rpc(action,value){startWorker();return new Promise((resolve,reject)=>{const id=++sequence;const timer=setTimeout(()=>{pending.delete(id);reject(Error('解锁计算超时，请刷新后重试'))},30000);pending.set(id,{resolve,reject,timer});cryptoWorker.postMessage({id,action,value})})}
async function deviceRecord(action,value){return new Promise((resolve,reject)=>{const open=indexedDB.open('market-report-device-v1',1);open.onupgradeneeded=()=>open.result.createObjectStore('device');open.onerror=()=>reject(open.error);open.onsuccess=()=>{const db=open.result,transaction=db.transaction('device',action==='get'?'readonly':'readwrite'),store=transaction.objectStore('device');const request=action==='get'?store.get('remembered'):action==='set'?store.put(value,'remembered'):store.delete('remembered');let result;request.onsuccess=()=>{result=request.result};transaction.oncomplete=()=>{db.close();resolve(result)};transaction.onerror=()=>{db.close();reject(transaction.error)}}})}
async function rememberDevice(){if(!globalThis.crypto?.subtle)return;const raw=await rpc('export',null);try{const wrapping=await crypto.subtle.generateKey({name:'AES-GCM',length:256},false,['encrypt','decrypt']),nonce=crypto.getRandomValues(new Uint8Array(12));const wrapped=await crypto.subtle.encrypt({name:'AES-GCM',iv:nonce},wrapping,raw);await deviceRecord('set',{key:wrapping,nonce,wrapped,salt:reportSalt});document.querySelector('#forget-device').hidden=false}finally{raw.fill(0)}}
async function packet(path){const url=new URL(path,location.href);url.search='encrypted=1'+(typeof DecompressionStream!=='undefined'?'&compressed=1':'');const abort=new AbortController(),timer=setTimeout(()=>abort.abort(),60000);try{const response=await fetch(url,{cache:'no-store',signal:abort.signal});if(!response.ok)throw Error('报告暂不可用，请稍后重试');return await response.json()}catch(error){if(error.name==='AbortError')throw Error('网络读取超时，请重试');throw error}finally{clearTimeout(timer)}}
async function unpack(path){const p=await packet(path);let data=await rpc('decrypt',p);if(p.compression==='gzip')data=new Uint8Array(await new Response(new Blob([data]).stream().pipeThrough(new DecompressionStream('gzip'))).arrayBuffer());return {data,type:p.type}}
async function image(img){if(!img.dataset.source||img.dataset.loaded)return;img.dataset.loaded='1';try{const value=await unpack(img.dataset.source);img.src=URL.createObjectURL(new Blob([value.data],{type:value.type}))}catch{img.alt='图形读取失败，刷新后重试';delete img.dataset.loaded}}
let copyTimer=null,copyTrigger=null;
function showCopied(name){const status=document.querySelector('#copy-status');clearTimeout(copyTimer);status.textContent='已复制 '+name;status.hidden=false;copyTimer=setTimeout(()=>{status.hidden=true},2500)}
function manualCopy(name,trigger){copyTrigger=trigger;const field=document.querySelector('#copy-value');field.value=name;document.querySelector('#copy-fallback').hidden=false;field.focus();field.select();field.setSelectionRange(0,name.length)}
async function copyCoin(button){const name=button.dataset.copyName;try{await navigator.clipboard.writeText(name);showCopied(name)}catch{manualCopy(name,button)}}
function installCopyNames(){const cards=[];for(const detail of report.querySelectorAll('details')){const summary=detail.querySelector(':scope > summary');if(!summary)continue;const label=summary.textContent.trim(),parts=label.split(' · '),symbol=parts[0];if(!symbol.endsWith('USDT'))continue;const name=symbol.slice(0,-4);if(!name)continue;const ends=Array.from(detail.querySelectorAll('tr')).slice(1).map(row=>row.cells[3]?.textContent.trim()||'').sort(),end=ends.at(-1)||'',start=parts.find(part=>part.startsWith('洗盘 '))?.slice(3)||'';cards.push({detail,end,start,symbol});const button=document.createElement('button');button.type='button';button.className='copy-symbol';button.dataset.copyName=symbol;button.setAttribute('aria-label','复制币名 '+symbol);button.title='点击复制 '+symbol;button.textContent=name;const quote=document.createElement('small');quote.textContent='USDT';button.append(quote);summary.replaceChildren(button,document.createTextNode((end?' · 洗盘结束 '+end:'')+label.slice(symbol.length)))}if(cards.length){cards.sort((a,b)=>{for(const field of ['end','start','symbol']){if(a[field]!==b[field])return a[field]<b[field]?1:-1}return 0});for(const card of cards)report.append(card.detail);const hint=document.createElement('p');hint.textContent='按洗盘结束时间从新到旧排列；同时间按洗盘开始时间、币名倒序。点击币名复制完整交易对（含 USDT）。';cards[0].detail.before(hint)}}
report.addEventListener('click',event=>{const button=event.target.closest('button[data-copy-name]');if(!button)return;event.preventDefault();event.stopPropagation();copyCoin(button)});
document.querySelector('#close-copy').onclick=()=>{document.querySelector('#copy-fallback').hidden=true;if(copyTrigger?.isConnected)copyTrigger.focus()};
async function show(path){const logical=path==='/'||path==='/index.html'?'/latest/index.html':path;const value=await unpack(logical);const parsed=new DOMParser().parseFromString(decoder.decode(value.data),'text/html');report.innerHTML=Array.from(parsed.head.querySelectorAll('style')).map(e=>e.outerHTML).join('')+parsed.body.innerHTML;history.replaceState(null,'',logical);installCopyNames();for(const img of report.querySelectorAll('img')){img.dataset.source=new URL(img.getAttribute('src'),location.href).pathname;img.removeAttribute('src');if(!img.closest('details')||img.closest('details').open)image(img)}for(const detail of report.querySelectorAll('details'))detail.addEventListener('toggle',()=>{if(detail.open)detail.querySelectorAll('img').forEach(image)});report.hidden=false;document.querySelector('#gate').hidden=true}
document.querySelector('#gate').addEventListener('submit',async event=>{event.preventDefault();if(busy)return;busy=true;const error=document.querySelector('#error'),button=event.currentTarget.querySelector('button'),input=document.querySelector('#password');button.disabled=true;button.textContent='正在验证…';error.style.color='#93c5fd';error.textContent='正在后台验证密码，页面仍可操作';try{await rpc('init',{password:input.value,salt:reportSalt});error.textContent='密码已验证，正在读取报告…';await show(location.pathname);input.value='';error.textContent='';try{await rememberDevice()}catch{const note=document.createElement('p');note.textContent='当前浏览器未能记住设备，本次查看正常。';report.prepend(note)}}catch(problem){error.style.color='#fca5a5';error.textContent=problem.message||'解锁失败，请重试'}finally{busy=false;button.disabled=false;button.textContent='打开报告'}});
document.querySelector('#forget-device').onclick=async()=>{await deviceRecord('delete');location.reload()};
(async()=>{if(!globalThis.crypto?.subtle)return;const error=document.querySelector('#error'),button=document.querySelector('#gate button');try{const stored=await deviceRecord('get');if(!stored)return;if(stored.salt!==reportSalt){await deviceRecord('delete');return}busy=true;button.disabled=true;error.style.color='#93c5fd';error.textContent='正在恢复本设备的解锁状态…';const raw=new Uint8Array(await crypto.subtle.decrypt({name:'AES-GCM',iv:stored.nonce},stored.key,stored.wrapped));try{await rpc('restore',raw)}finally{raw.fill(0)}await show(location.pathname);document.querySelector('#forget-device').hidden=false;error.textContent=''}catch{await deviceRecord('delete').catch(()=>{});error.textContent='请重新输入密码'}finally{busy=false;button.disabled=false}})();
report.addEventListener('click',async event=>{const link=event.target.closest('a');if(!link)return;const url=new URL(link.getAttribute('href'),location.href);if(url.origin!==location.origin)return;event.preventDefault();try{if(url.pathname.endsWith('.html')||url.pathname==='/history'||url.pathname==='/'){await show(url.pathname);return}const value=await unpack(url.pathname);const blob=URL.createObjectURL(new Blob([value.data],{type:value.type}));if(value.type==='image/svg+xml'){document.querySelector('#preview img').src=blob;document.querySelector('#preview').hidden=false}else{const a=document.createElement('a');a.href=blob;a.download=url.pathname.split('/').pop();a.click();setTimeout(()=>URL.revokeObjectURL(blob),1000)}}catch{alert('读取失败，请稍后重试')}});
document.querySelector('#close-preview').onclick=()=>{const img=document.querySelector('#preview img');URL.revokeObjectURL(img.src);img.removeAttribute('src');document.querySelector('#preview').hidden=true};
</script></html>'''


def encrypted_packet(content: bytes, kind: str, credential: dict, compress=False) -> bytes:
    nonce = os.urandom(12)
    key = base64.b64decode(credential["key"])
    if compress:
        content = gzip.compress(content, mtime=0)
    return json.dumps({"salt": credential["salt"], "nonce": base64.b64encode(nonce).decode(),
                       "ciphertext": base64.b64encode(AESGCM(key).encrypt(nonce, content, None)).decode(),
                       "type": kind, "compression": 'gzip' if compress else None, "iterations": 210000, "version": 1}).encode()


def latest_report(root: Path) -> Path:
    # 扫描中的部分结果留在历史页，首页始终优先显示最新一轮完整扫描。
    completed = []
    for path in (root / "runs").glob("live-*/summary.json"):
        if not re.fullmatch(r"live-[0-9a-f]{20}", path.parent.name):
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if data.get("status") == "complete" and (path.parent / "index.html").is_file():
            completed.append((data.get("end_bj_exclusive", ""), data.get("updated_at_bj", ""), path.parent))
    return max(completed)[2] if completed else root / "latest"


def report_path(root: Path, url: str) -> Path | None:
    path = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
    if path in ("/", "/index.html"):
        path = "/latest/index.html"
    if path == "/status.json":
        return root / "status.json"
    parts = path.strip("/").split("/")
    if parts[0] == "latest":
        relative = parts[1:]
        folder = latest_report(root)
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


def handler(root: Path, credential: dict | None, redirect_https=False):
    class ReportHandler(BaseHTTPRequestHandler):
        def serve(self, head=False):
            path = urllib.parse.urlsplit(self.path).path
            if path.startswith('/.well-known/acme-challenge/'):
                token = path.removeprefix('/.well-known/acme-challenge/')
                target = root/'acme-challenges/.well-known/acme-challenge'/token
                if not re.fullmatch(r'[a-zA-Z0-9_-]+', token) or not target.is_file():
                    self.send_error(404)
                    return
                return self.respond(target.read_bytes(), 'text/plain', head)
            if redirect_https:
                self.send_response(301)
                self.send_header('Location', 'https://129.226.223.91:80'+self.path)
                self.end_headers()
                return
            if path == '/report_crypto.js':
                content = Path(__file__).with_name('report_crypto.js').read_bytes()
                kind = 'application/javascript'
                return self.respond(content, kind, head)
            if urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get('encrypted') != ['1']:
                crypto = base64.b64encode(Path(__file__).with_name('report_crypto.js').read_bytes()).decode()
                gate = GATE.replace('__REPORT_SALT__', credential['salt']).replace('__CRYPTO_BASE64__', crypto)
                return self.respond(gate.encode(), 'text/html; charset=utf-8', head)
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
                    # 将图形与下载固定到本轮目录，下一轮完成时已打开的报告仍能读取原文件。
                    prefix = ('/' + target.parent.relative_to(root).as_posix() + '/').encode()
                    content = re.sub(rb'((?:href|src)=")(?![a-z]+:|/|#)([^\"]+)"',
                                     lambda match: match[1] + prefix + match[2] + b'"', content)
                    navigation = '<nav><a href="/">最新扫描</a> | <a href="/history">历史扫描与回测</a> | <a href="/status.json">运行状态</a></nav>'.encode()
                    if path in ("/", "/index.html", "/latest/index.html"):
                        note = "自动扫描：北京时间 00:00、04:00、08:00、12:00、16:00、20:00；扫描期间保留上一轮完整结果。"
                        try:
                            progress = json.loads((root / "status.json").read_text())
                            if progress.get("status") == "running" and progress.get("scope") != target.parent.name:
                                note += f" 新一轮进行中：{progress.get('completed_symbols', 0)}/{progress.get('total_symbols', 0)} 个交易对。"
                        except (OSError, ValueError):
                            pass
                        navigation += ('<p>' + html.escape(note) + '</p>').encode()
                    content = content.replace(b"<h1>", navigation + b"<h1>", 1)
            compress = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get('compressed') == ['1']
            return self.respond(encrypted_packet(content, kind, credential, compress), 'application/json', head)

        def respond(self, content, kind, head):
            compressed = kind.startswith('text/html') and 'gzip' in self.headers.get('Accept-Encoding', '')
            if compressed:
                content = gzip.compress(content, mtime=0)
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if compressed:
                self.send_header('Content-Encoding', 'gzip')
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
    parser.add_argument('--redirect-https', action='store_true')
    parser.add_argument('--tls', action='store_true')
    args = parser.parse_args()
    credential = None
    if not args.redirect_https:
        credential_path = Path(os.environ['CREDENTIALS_DIRECTORY']) / 'report-key'
        credential = json.loads(credential_path.read_text())
        if len(base64.b64decode(credential['key'])) != 32:
            raise SystemExit('报表加密凭据无效。')
    handler_class = handler(Path(args.root).resolve(), credential, args.redirect_https)
    if args.tls:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(Path(os.environ['CREDENTIALS_DIRECTORY'])/'tls-pem'))
        server = TLSHTTPServer((args.bind, args.port), handler_class, context)
    else:
        server = ThreadingHTTPServer((args.bind, args.port), handler_class)
    server.serve_forever()
