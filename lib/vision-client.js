const { spawn, execFile } = require('node:child_process')
const path = require('node:path')
const { EventEmitter } = require('node:events')
const readline = require('node:readline')

const ROOT = path.join(__dirname, '..')
const SCRIPT = path.join(ROOT, 'vision', 'main.py')

// Windows 上解释器叫 python，Linux / 树莓派上是 python3。
// 两个都试，并且要求能 import cv2——装了 python 但没装 opencv 的情况很常见。
const CANDIDATES = process.platform === 'win32'
  ? ['python', 'python3']
  : ['python3', 'python']

const PROBE = 'import sys, cv2; print(sys.version.split()[0], cv2.__version__)'
// 单帧上限。识别是一次性的，而 analyze() 里还要顺带解一次碎片拼接（默认预算 12s，
// 见 vision/pipeline.py 的 ASSEMBLY_TIME_BUDGET），所以要留出足够余量，
// 否则树莓派这种慢机器上会「分析还没算完就被判超时」。
const FRAME_TIMEOUT_MS = 45000

/**
 * 视觉侧车的进程客户端。
 *
 * 协议：每帧 = 4 字节小端长度 + JPEG 字节，写进 Python 的 stdin；
 * Python 每处理完一帧往 stdout 回一行 JSON。日志走 stderr，不污染协议。
 *
 * 用 stdio 而不是本地端口：不占端口、Windows 上不弹防火墙、进程随 Electron 生死。
 */
class VisionClient extends EventEmitter {
  constructor() {
    super()
    this.proc = null
    this.python = null
    this.version = null
    // 结果按发出顺序返回，用队列一一对应，避免快慢不一时张冠李戴
    this.waiters = []
    this.pending = null
    this.lastError = null
  }

  /** 找一个能用的 Python。找不到返回 null，并留下原因供界面显示。 */
  async findPython() {
    if (this.python) return this.python
    const failures = []
    for (const candidate of CANDIDATES) {
      try {
        const output = await new Promise((resolve, reject) => {
          execFile(candidate, ['-c', PROBE], { timeout: 10000, windowsHide: true },
            (error, stdout) => (error ? reject(error) : resolve(stdout)))
        })
        const [pyVersion, cvVersion] = output.trim().split(/\s+/)
        this.python = candidate
        this.version = { python: pyVersion, opencv: cvVersion }
        this.emit('log', `找到 ${candidate} (Python ${pyVersion}, OpenCV ${cvVersion})`)
        return candidate
      } catch (error) {
        failures.push(`${candidate}: ${(error.stderr || error.message).toString().trim().split('\n')[0]}`)
      }
    }
    this.lastError = `没找到带 OpenCV 的 Python。试过：\n  ${failures.join('\n  ')}\n` +
      '装依赖：python -m pip install -r vision/requirements.txt'
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
        // 不缓冲：否则结果会攒在 Python 的缓冲区里，界面看着像卡住
        PYTHONUNBUFFERED: '1',
        // 中文日志按 UTF-8 出来，避免 Windows 上按 cp936 编码变乱码
        PYTHONIOENCODING: 'utf-8',
      },
      stdio: ['pipe', 'pipe', 'pipe'],
    })

    // stdout 只走协议：NDJSON，一行一个结果
    readline.createInterface({ input: this.proc.stdout }).on('line', (line) => {
      const trimmed = line.trim()
      if (!trimmed) return
      let message
      try {
        message = JSON.parse(trimmed)
      } catch {
        this.emit('log', `侧车输出了非 JSON 内容：${trimmed.slice(0, 200)}`)
        return
      }
      const waiter = this.waiters.shift()
      if (waiter) {
        clearTimeout(waiter.timer)
        waiter.resolve(message)
      } else {
        this.emit('result', message) // 没人等（比如界面刚关掉），直接广播
      }
    })

    readline.createInterface({ input: this.proc.stderr }).on('line', (line) => {
      if (line.trim()) this.emit('log', line.trim())
    })

    this.proc.on('exit', (code, signal) => {
      const reason = signal ? `信号 ${signal}` : `退出码 ${code}`
      this.emit('log', `视觉侧车结束（${reason}）`)
      this.proc = null
      const error = new Error(`视觉侧车已退出（${reason}）`)
      this.waiters.forEach((waiter) => {
        clearTimeout(waiter.timer)
        waiter.reject(error)
      })
      this.waiters = []
      this.emit('stopped')
    })

    this.proc.on('error', (error) => {
      this.lastError = `无法启动视觉侧车：${error.message}`
      this.emit('log', this.lastError)
    })

    return true
  }

  /**
   * 送一帧进去，等这一帧的结果。
   * 调用方 await 完再送下一帧，慢的时候自然丢帧，不会堆积。
   */
  analyze(jpegBuffer) {
    if (!this.proc) return Promise.reject(new Error('视觉侧车没在运行'))

    const header = Buffer.alloc(4)
    header.writeUInt32LE(jpegBuffer.length, 0)
    this.proc.stdin.write(Buffer.concat([header, jpegBuffer]))

    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.waiters = this.waiters.filter((w) => w.timer !== timer)
        reject(new Error(`单帧处理超过 ${FRAME_TIMEOUT_MS}ms，已放弃`))
      }, FRAME_TIMEOUT_MS)
      this.waiters.push({ resolve, reject, timer })
    })
  }

  stop() {
    if (this.proc) {
      this.proc.stdin.end() // 触发 Python 侧读到 EOF，干净退出
      this.proc = null
    }
  }
}

module.exports = { VisionClient }
