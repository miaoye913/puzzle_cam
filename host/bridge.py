"""
STM32 拼图机的桥接进程。

Electron 主进程 spawn 它，两边用 JSON 行说话：

    stdin   一行一个 JSON   = 命令
    stdout  一行一个 JSON   = 事件 / 回复
    stderr  任意文本        = 日志

真正干活的是 puzzle_host/ 里那套（MIT，见 puzzle_host/LICENSE）：`link.make_link` 负责
打开串口或 TCP，`client.ProtoClient` 负责帧编解码 + 后台读线程 + 事件回调。
**这里只做翻译，不重复实现协议**——那套代码有 76 项测试覆盖，自己再写一遍没有任何好处。

连接目标既可以是真串口（Linux 上 /dev/ttyUSB0、Windows 上 COM3），也可以是
`tcp://127.0.0.1:5000` —— 后者指向 `puzzle_host/demo_device.py` 起的仿真 STM32，
所以**整条链路不接硬件也能端到端测**。
"""

import json
import os
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_PUZZLE_HOST = os.path.join(os.path.dirname(_HERE), 'puzzle_host')
# 把 vendored 的那套放到最前面：它是一堆平铺的模块（protocol.py / link.py / client.py），
# 而且自带一份 pyserial 在 vendor/ 下（没装 pyserial 的机器也能用）
for _path in (os.path.join(_PUZZLE_HOST, 'vendor'), _PUZZLE_HOST):
    if os.path.isdir(_path) and _path not in sys.path:
        sys.path.insert(0, _path)

try:
    import link as host_link          # noqa: E402
    import client as host_client      # noqa: E402
    import protocol as host_protocol  # noqa: E402
except ImportError as error:          # pragma: no cover - 只会在 vendor 被破坏时发生
    print(f'导入 puzzle_host 失败：{error}', file=sys.stderr)
    sys.exit(2)

DEFAULT_BAUDRATE = 115200

_out_lock = threading.Lock()


def emit(payload):
    """往 stdout 写一行 JSON。读线程和主线程都会调，所以要加锁。"""
    line = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
    with _out_lock:
        sys.stdout.write(line + '\n')
        sys.stdout.flush()


def log(text):
    print(text, file=sys.stderr, flush=True)


def force_utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass


class Bridge:
    """一条连接 + 一个客户端 + 一个轮询线程。同一时刻只维持一条连接。"""

    def __init__(self):
        self.link = None
        self.client = None
        self.target = None
        self._poll_stop = None
        self._poll_thread = None

    # ── 连接 ────────────────────────────────────────────────────────
    def open(self, target, baudrate=DEFAULT_BAUDRATE):
        if self.client is not None:
            self.close()
        self.link = host_link.make_link(target, baudrate=baudrate)
        self.link.open()                       # 真正建立连接，失败会抛 LinkError
        # target 要在 start() 之前设好：ProtoClient 一 start 就会抛一个 conn 事件出来，
        # 那时候 _on_event 要拿它填进 JSON
        self.target = target
        self.client = host_client.ProtoClient(self.link, on_event=self._on_event,
                                              auto_reopen=True)
        self.client.start()

    def close(self):
        self.stop_poll()
        if self.client is not None:
            try:
                self.client.close()
            except Exception as error:          # noqa: BLE001 - 关连接失败不该拖垮进程
                log(f'关闭连接时出错：{error}')
        self.client = None
        self.link = None
        self.target = None
        emit({'event': 'conn', 'state': 'down'})

    # ── 把他们的 Event 翻成 JSON 行 ─────────────────────────────────
    def _on_event(self, event):
        kind = getattr(event, 'kind', '')
        if kind == 'status' and event.status is not None:
            payload = event.status.as_dict()
            payload['event'] = 'status'
            emit(payload)
        elif kind == 'conn':
            emit({'event': 'conn', 'state': event.state, 'target': self.target})
        elif kind == 'echo':
            emit({'event': 'echo', 'data': event.data.hex(' ')})
        elif kind == 'error':
            emit({'event': 'error', 'message': event.message})
        elif kind in ('tx', 'rx'):
            # 原始字节流：界面上的收发日志用。解析好的状态另有 status 事件
            emit({'event': 'log', 'dir': kind, 'text': event.text()})
        else:
            emit({'event': 'log', 'text': event.text()})

    # ── 周期查状态 ──────────────────────────────────────────────────
    # ACK 只代表「命令排队了」，不代表动作做完，所以实时状态必须靠周期 GET ALL 刷
    def start_poll(self, interval_ms):
        self.stop_poll()
        if interval_ms <= 0:
            return
        self._poll_stop = threading.Event()

        def loop():
            # 先歇一轮再查第一遍，免得刚下发的命令还没执行就查
            while not self._poll_stop.wait(interval_ms / 1000.0):
                if self.client is None:
                    return
                try:
                    self.client.get_all()
                except Exception as error:      # noqa: BLE001 - 掉线不该让轮询线程死掉
                    emit({'event': 'error', 'message': f'轮询失败：{error}'})
                    return

        self._poll_thread = threading.Thread(target=loop, name='host-poll', daemon=True)
        self._poll_thread.start()

    def stop_poll(self):
        if self._poll_stop is not None:
            self._poll_stop.set()
            self._poll_stop = None
        self._poll_thread = None

    # ── 命令分发 ────────────────────────────────────────────────────
    def handle(self, message):
        cmd = message.get('cmd')
        try:
            if cmd == 'ports':
                ports = [{'device': device, 'description': desc}
                         for device, desc in host_link.list_serial_ports()]
                emit({'ok': True, 'cmd': cmd, 'ports': ports,
                      'havePyserial': bool(getattr(host_link, 'HAVE_PYSERIAL', False))})

            elif cmd == 'open':
                self.open(message.get('target', ''), int(message.get('baud', DEFAULT_BAUDRATE)))
                emit({'ok': True, 'cmd': cmd})

            elif cmd == 'close':
                self.close()
                emit({'ok': True, 'cmd': cmd})

            elif cmd == 'set':
                self._require_client()
                done = []
                if 'x' in message:
                    self.client.set_x(int(message['x'])); done.append('x')
                if 'y' in message:
                    self.client.set_y(int(message['y'])); done.append('y')
                if 'rot' in message:
                    self.client.set_rot(int(message['rot'])); done.append('rot')
                if 'servo' in message:
                    self.client.set_servo(bool(int(message['servo']))); done.append('servo')
                if 'em' in message:
                    self.client.set_em(bool(int(message['em']))); done.append('em')
                emit({'ok': True, 'cmd': cmd, 'sent': done})

            elif cmd == 'ping':
                self._require_client()
                self.client.ping()
                emit({'ok': True, 'cmd': cmd})

            elif cmd == 'reinit':
                self._require_client()
                self.client.reinit()
                emit({'ok': True, 'cmd': cmd})

            elif cmd == 'get_all':
                self._require_client()
                status = self.client.get_all()
                payload = status.as_dict() if status is not None else None
                emit({'ok': payload is not None, 'cmd': cmd, 'status': payload})

            elif cmd == 'echo':
                self._require_client()
                data = host_protocol.parse_hex_bytes(message.get('data', ''))
                back = self.client.echo(data)
                emit({'ok': back is not None, 'cmd': cmd,
                      'data': back.hex(' ') if back is not None else None})

            elif cmd == 'poll':
                self.start_poll(int(message.get('interval_ms', 0)))
                emit({'ok': True, 'cmd': cmd})

            else:
                emit({'ok': False, 'cmd': cmd, 'error': f'未知命令：{cmd!r}'})
        except host_link.LinkError as error:
            emit({'ok': False, 'cmd': cmd, 'error': f'串口错误：{error}'})
        except (host_client.ResponseError, host_client.ResponseTimeout) as error:
            emit({'ok': False, 'cmd': cmd, 'error': f'设备没有正确应答：{error}'})
        except (ValueError, TypeError) as error:
            emit({'ok': False, 'cmd': cmd, 'error': f'参数不对：{error}'})

    def _require_client(self):
        if self.client is None:
            raise host_link.LinkError('还没连接')


def main():
    force_utf8()
    bridge = Bridge()
    stdin = sys.stdin
    while True:
        line = stdin.readline()
        if not line:                       # Electron 关掉管道了
            break
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as error:
            emit({'ok': False, 'error': f'命令不是合法 JSON：{error}'})
            continue
        try:
            bridge.handle(message)
        except Exception as error:          # noqa: BLE001 - 单条命令失败不该让桥接退出
            emit({'ok': False, 'cmd': message.get('cmd'),
                  'error': f'{type(error).__name__}: {error}'})
    try:
        bridge.close()
    except Exception:                       # noqa: BLE001 - 退出路径，尽力而为
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
