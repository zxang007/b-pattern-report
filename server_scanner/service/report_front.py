#!/usr/bin/env python3
from __future__ import annotations

import select
import socket
import socketserver


class Front(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(15)
        # 已开放的 80 端口同时接受 HTTP 跳转和标准 TLS 连接，避免依赖云安全组新增端口。
        first = self.request.recv(1, socket.MSG_PEEK)
        if not first:
            return
        destination = 443 if first == b'\x16' else 18765
        with socket.create_connection(('127.0.0.1', destination), timeout=15) as backend:
            sockets = [self.request, backend]
            for item in sockets:
                item.setblocking(False)
            buffers = {self.request: bytearray(), backend: bytearray()}
            reading = set(sockets)
            closed_writes = set()
            while reading or any(buffers.values()):
                readers = [item for item in reading if len(buffers[backend if item is self.request else self.request]) < 1048576]
                writers = [item for item in sockets if buffers[item]]
                ready, writable, _ = select.select(readers, writers, [], 30)
                if not ready and not writable:
                    return
                for source in ready:
                    try:
                        data = source.recv(65536)
                    except BlockingIOError:
                        continue
                    if not data:
                        reading.remove(source)
                        continue
                    target = backend if source is self.request else self.request
                    buffers[target].extend(data)
                for target in writable:
                    try:
                        count = target.send(buffers[target])
                        del buffers[target][:count]
                    except BlockingIOError:
                        pass
                for target in sockets:
                    source = backend if target is self.request else self.request
                    if source not in reading and not buffers[target] and target not in closed_writes:
                        try:
                            target.shutdown(socket.SHUT_WR)
                        except OSError:
                            pass
                        closed_writes.add(target)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == '__main__':
    Server(('0.0.0.0', 80), Front).serve_forever()
