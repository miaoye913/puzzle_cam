const { spawn, execFile } = require('node:child_process')
const path = require('node:path')
const { EventEmitter } = require('node:events')
const readline = require('node:readline')

const ROOT = path.join(__dirname, '..')
const SCRIPT = path.join(ROOT, 'host', 'bridge.py')
const PUZZLE_HOST = path.join(ROOT, 'puzzle_host')
const VENDOR = path.join(PUZZLE_HOST, 'vendor')

// 和 vision-client 同一套候选顺序：Windows 上叫 python，Linux / 树莓派上叫 python3
const CANDIDATES = process.platform === 'win32'
  ? ['python', 'python3']
  : ['python3', 'python']

// 探针顺带验一下 vendored 的那套能不能 import——只检查「有没有 python」是不够的，
// puzzle_host 被搬坏了也要在这里就发现，而不是等到用户点连接才报错
const PROBE = 'import sys; [sys.path.insert(0, p) for p in reversed(sys.argv[1:])]; ' +
  'import protocol, link, client; print(sys.version.split()[0])'

// 单条命令的等待上限。开串口偶尔会慢，给宽一点
const COMMAND_TIMEOUT_MS = 10000

/**
 * STM32 拼图机的进程客户端。
 *
 * 桥接进程（host/bridge.py）两边都说 JSON，一行一个：stdin 收命令、stdout 发事件和回复、
 * stderr 走日志。命令之间用「先进先出」对上号——桥接那边是单线程顺序处理的，回复必然同序。
 *
 * 用 stdio 而不是自己开串口：渲染进程碰不到串口，主进程要开串口就得引原生模块
 * （serialport 三个平台各自要编译）。而复用 puzzle_host 那套还有个额外好处——
 * 它的帧协议有 76 项测试覆盖，自己再实现一遍纯属冒险。
 */
class HostClient extends EventEmitter {
  constructor() {
    super()
    this.proc = null
    this.python = null
    this.version = null
    this.waiters = []
    this.lastError = null
  }

  async findPython() {
    if (this.python) return this.python
    const failures = []
    for (const candidate of CANDIDATES) {
      try {
        const output = await new Promise((resolve, reject) => {
          execFile(candidate, ['-c', PROBE, VENDOR, PUZZLE_HOST],
            { timeout: 10000, windowsHide: true },
            (error, stdout) => (error ? reject(error) : resolve(stdout)))
        })
        this.python = candidate
        this.version = output.trim()
        this.emit('log', `找到 ${candidate} (Python ${this.version})，puzzle_host 可导入`)
        return candidate
      } catch (error) {
        const detail = (error.stderr || error.message).toString().trim().split('\n').pop()
        failures.push(`${candidate}: ${detail}`)
      }
    }
    this.lastError = `没找到能用的 Python（要能导入 puzzle_host）：\n  ${failures.join('\n  ')}`
    this.emit('log', this.lastError)
    return null
  }

  async start() {
    if (this.proc) return true
    if (!(await this.findPython())) return false

    this.proc = spawn(this.python, [SCRIPT], {
      cwd: ROOT,
      windowsHide: true,
      env: {
        ...process.env,
        PYTHONUNBUFFERED: '1',
        PYTHONIOENCODING: 'utf-8',
      },
      stdio: ['pipe', 'pipe', 'pipe'],
    })

    readline.createInterface({ input: this.proc.stdout }).on('line', (line) => {
      const trimmed = line.trim()
      if (!trimmed) return
      let message
      try {
        message = JSON.parse(trimmed)
      } catch {
        this.emit('log', `桥接输出了非 JSON 内容：${trimmed.slice(0, 200)}`)
        return
      }
      // 带 event 的是主动推上来的（状态、收发日志、掉线…），其余是对命令的回复
      if (message.event) {
        this.emit('event', message)
        return
      }
      const waiter = this.waiters.shift()
      if (waiter) {
        clearTimeout(waiter.timer)
        waiter.resolve(message)
      } else {
        this.emit('event', message)
      }
    })

    readline.createInterface({ input: this.proc.stderr }).on('line', (line) => {
      if (line.trim()) this.emit('log', line.trim())
    })

    this.proc.on('exit', (code, signal) => {
      const reason = signal ? `信号 ${signal}` : `退出码 ${code}`
      this.emit('log', `串口桥接结束（${reason}）`)
      this.proc = null
      const error = new Error(`串口桥接已退出（${reason}）`)
      this.waiters.forEach((waiter) => {
        clearTimeout(waiter.timer)
        waiter.reject(error)
      })
      this.waiters = []
      this.emit('event', { event: 'conn', state: 'down' })
      this.emit('stopped')
    })

    this.proc.on('error', (error) => {
      this.lastError = `无法启动串口桥接：${error.message}`
      this.emit('log', this.lastError)
    })

    return true
  }

  /** 发一条命令，等它的回复。 */
  command(payload) {
    if (!this.proc) return Promise.reject(new Error('串口桥接没在运行'))
    this.proc.stdin.write(JSON.stringify(payload) + '\n')
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.waiters = this.waiters.filter((w) => w.timer !== timer)
        reject(new Error(`命令 ${payload.cmd} 超过 ${COMMAND_TIMEOUT_MS}ms 没有回复`))
      }, COMMAND_TIMEOUT_MS)
      this.waiters.push({ resolve, reject, timer })
    })
  }

  stop() {
    if (this.proc) {
      this.proc.stdin.end() // 桥接读到 EOF 就自己收尾退出
      this.proc = null
    }
  }
}

module.exports = { HostClient }
