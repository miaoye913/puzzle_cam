/**
 * 冒烟测试：用 Electron 静默加载真实界面，检查
 *   1. 页面无 JS 报错，preload 桥接生效
 *   2. 控件初始化正常（分辨率选项、设备下拉）
 *   3. getUserMedia 能否真的取到流（无摄像头记 WARN，不算失败）
 *
 * 运行：npm run smoke
 */
const { app, BrowserWindow, session } = require('electron')
const fs = require('node:fs/promises')
const path = require('node:path')
const { writeSnapshot } = require('../lib/snapshot')
const { VisionClient } = require('../lib/vision-client')
const { HostClient } = require('../lib/host-client')
const { registerIpcHandlers } = require('../lib/ipc')

const ROOT = path.join(__dirname, '..')
const problems = []
const warnings = []

/**
 * 视觉链路：真开摄像头 → 在渲染进程里抽帧压 JPEG → 走真实 IPC → Python 侧车 → 拿回结果。
 * 用真实接线（registerIpcHandlers）而不是在这里复刻一份，否则线上接线坏了测试照样绿。
 */
async function checkVision(win) {
  const vision = new VisionClient()
  vision.on('log', (line) => console.log(`  [vision] ${line}`))
  const host = new HostClient()
  host.on('log', (line) => console.log(`  [host] ${line}`))
  // 两个客户端一次注册完——ipcMain.handle 同一个通道注册第二次会直接抛错
  registerIpcHandlers({ vision, host, getWindow: () => win })

  const started = await win.webContents.executeJavaScript('window.camAPI.vision.start()')
  if (!started) {
    problems.push('Python 侧车起不来（见上面日志）')
    return
  }

  // 相机若不可用就跳过，不要因为环境问题假报失败
  const camOk = await win.webContents.executeJavaScript(`(async () => {
    try {
      const s = await navigator.mediaDevices.getUserMedia({ video: { width: { ideal: 1280 } }, audio: false })
      const v = document.getElementById('video')
      v.srcObject = s
      await v.play()
      await new Promise(r => setTimeout(r, 600))
      return true
    } catch { return false }
  })()`)
  if (!camOk) {
    warnings.push('相机不可用，视觉链路只跑了离线图片那部分')
    return
  }

  // 复用渲染进程里真实的抽帧 + 投影函数，避免测试和线上两套逻辑
  const reply = await win.webContents.executeJavaScript(`(async () => {
    const captured = await captureForVision()
    if (!captured) return { ok: false, error: '抽帧失败' }
    const reply = await window.camAPI.vision.analyze(captured.bytes)
    return { ...reply, bytes: captured.bytes.length, mapping: captured.mapping }
  })()`)

  if (!reply.ok) {
    problems.push(`视觉分析失败：${reply.error}`)
    return
  }

  const r = reply.result
  console.log(`  vision jpeg : ${reply.bytes} bytes → ${r.frame.width}x${r.frame.height}`)
  console.log(`  vision board: ${r.board.found ? '已定位' : '未找到'} · ${r.pieces.length} 块 · ${r.timing_ms}ms`)

  if (!r.frame || !r.frame.width) problems.push('结果里没有 frame 尺寸，叠加层无法换算坐标')
  if (r.board.found && (r.board.corners ?? []).length !== 4) problems.push('拼板四角数量不是 4')

  // 板面在画布里的摆放是纯计算，直接验一遍边界情况：
  // 要按宽高比装进可用区域、居中，而且 (0,0) 和 (板宽,板高) 正好落在画布矩形上。
  // 主画面现在就是矫正后的板面，坐标只差这一个缩放，所以这条最值得卡住。
  if (reply.result?.board?.rect_size) {
    const [bw, bh] = reply.result.board.rect_size
    const fit = await win.webContents.executeJavaScript(`(() => {
      const f = fitBoard(${bw}, ${bh}, 800, 450)
      const proj = (x, y) => [f.left + x * f.scale, f.top + y * f.scale]
      const [x0, y0] = proj(0, 0)
      const [x1, y1] = proj(${bw}, ${bh})
      return { ...f, x0, y0, x1, y1 }
    })()`)
    if (fit.w > 800 || fit.h > 450) {
      problems.push(`板面没装进可用区域：${fit.w}x${fit.h} 超出了 800x450`)
    }
    const aspectIn = bw / bh
    const aspectOut = fit.w / fit.h
    if (Math.abs(aspectIn - aspectOut) / aspectIn > 0.02) {
      problems.push(`板面宽高比被压变形了：${aspectIn.toFixed(3)} → ${aspectOut.toFixed(3)}`)
    }
    if (Math.abs(fit.x0 - fit.left) > 1 || Math.abs(fit.y0 - fit.top) > 1 ||
        Math.abs(fit.x1 - (fit.left + fit.w)) > 1 ||
        Math.abs(fit.y1 - (fit.top + fit.h)) > 1) {
      problems.push(`板面角点没落在画布矩形上：${JSON.stringify(fit)}`)
    }
    console.log(`  board fit   : ${bw}x${bh} → ${fit.w}x${fit.h} @ (${fit.left},${fit.top})`)
  }

  // 主画面得有东西可画：侧车必须把矫正后的板面图带回来
  if (reply.result?.board?.found && !reply.result.board_image_b64) {
    problems.push('板子找到了但没带板面图，主画面会是空的')
  }

  await win.webContents.executeJavaScript('window.camAPI.vision.stop()')
  await checkHostConsole(win)
}

/**
 * 串口控制台：验桥接能起来、能列串口。
 *
 * 收发那一段要有仿真设备才验得动（`python puzzle_host/demo_device.py --port 5000`），
 * 没有就记 WARN 跳过——环境没准备好不该报成代码坏了。
 */
async function checkHostConsole(win) {
  const started = await win.webContents.executeJavaScript('window.camAPI.host.start()')
  if (!started) {
    warnings.push('串口桥接起不来（没找到能导入 puzzle_host 的 Python）')
    return
  }

  const ports = await win.webContents.executeJavaScript(`(async () => {
    const r = await window.camAPI.host.command({ cmd: 'ports' })
    return { ok: r.ok, error: r.error, count: r.reply?.ports?.length ?? 0, pyserial: r.reply?.havePyserial }
  })()`)
  if (!ports.ok) {
    problems.push(`串口列表拿不到：${ports.error}`)
  } else {
    console.log(`  host bridge : OK · ${ports.count} 个串口 · pyserial ${ports.pyserial ? '可用' : '不可用'}`)
  }

  const linked = await win.webContents.executeJavaScript(`(async () => {
    const r = await window.camAPI.host.command({ cmd: 'open', target: 'tcp://127.0.0.1:5000' })
    if (!r.reply?.ok) return { ok: false, error: r.reply?.error }
    const seen = new Promise((resolve) => {
      const timer = setTimeout(() => resolve(false), 3000)
      window.camAPI.host.onEvent((m) => { if (m.event === 'status') { clearTimeout(timer); resolve(true) } })
    })
    await window.camAPI.host.command({ cmd: 'set', x: 777 })
    await window.camAPI.host.command({ cmd: 'get_all' })
    const all = await window.camAPI.host.command({ cmd: 'get_all' })
    const got = await seen
    await window.camAPI.host.command({ cmd: 'close' })
    return { ok: true, sawEvent: got, posX: all.reply?.status?.posX }
  })()`)

  if (!linked.ok) {
    warnings.push(`没有 tcp://127.0.0.1:5000 的仿真设备，串口收发只验到「列串口」（${linked.error}）`)
  } else {
    if (linked.posX !== 777) problems.push(`仿真设备回的 X 是 ${linked.posX}，应为 777`)
    if (!linked.sawEvent) problems.push('status 事件没转发到渲染进程')
    console.log(`  host link   : OK · 下发并读回 X=${linked.posX} · status 事件${linked.sawEvent ? '已收到' : '没收到'}`)
  }
}

/** 让渲染进程真画一张 canvas，走完整的 dataURL → 解码 → 落盘链路 */
async function checkSnapshotWrite(win) {
  const dataUrl = await win.webContents.executeJavaScript(`(() => {
    const c = document.createElement('canvas')
    c.width = 8; c.height = 8
    const ctx = c.getContext('2d')
    ctx.fillStyle = '#4c8dff'; ctx.fillRect(0, 0, 8, 8)
    return c.toDataURL('image/png')
  })()`)

  const target = path.join(app.getPath('temp'), `puzzle-cam-smoke-${Date.now()}.png`)
  try {
    const { bytes } = await writeSnapshot(dataUrl, target)
    const onDisk = await fs.readFile(target)
    const isPng = onDisk.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))
    console.log(`  snapshot    : ${bytes} bytes, PNG 签名 ${isPng ? 'OK' : 'FAIL'}`)
    if (bytes !== onDisk.length) problems.push('落盘字节数与解码结果不一致')
    if (!isPng) problems.push('落盘文件不是合法 PNG')
  } catch (error) {
    problems.push(`拍照保存失败: ${error.message}`)
  } finally {
    await fs.rm(target, { force: true })
  }

  // 非法输入必须被拒，避免把任意内容当图片写盘
  try {
    await writeSnapshot('data:text/html;base64,PHNjcmlwdD4=', target)
    problems.push('非 PNG 的 dataURL 未被拒绝')
  } catch {
    /* 预期路径 */
  }
}

app.whenReady().then(async () => {
  session.defaultSession.setPermissionRequestHandler((_wc, p, cb) => cb(p === 'media' || p === 'videoCapture'))
  session.defaultSession.setPermissionCheckHandler((_wc, p) => p === 'media' || p === 'videoCapture')

  const win = new BrowserWindow({
    show: false,
    webPreferences: {
      preload: path.join(ROOT, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  })

  win.webContents.on('console-message', (_e, level, message, line, sourceId) => {
    if (level >= 2) problems.push(`console[${level}] ${message} (${path.basename(sourceId ?? '')}:${line})`)
  })
  win.webContents.on('did-fail-load', (_e, code, desc) => problems.push(`did-fail-load ${code} ${desc}`))
  win.webContents.on('render-process-gone', (_e, details) => problems.push(`renderer gone: ${details.reason}`))
  win.webContents.on('preload-error', (_e, file, error) => problems.push(`preload error ${file}: ${error.message}`))

  try {
    await win.loadFile(path.join(ROOT, 'renderer', 'index.html'))

    // 等渲染脚本完成一次设备枚举
    await new Promise((r) => setTimeout(r, 800))

    const dom = await win.webContents.executeJavaScript(`(() => ({
      bridge: typeof window.camAPI?.saveSnapshot === 'function',
      resolutions: document.getElementById('resolution').options.length,
      toggleLabel: document.getElementById('toggle').textContent.trim(),
      placeholderVisible: !document.getElementById('placeholder').classList.contains('hidden'),
    }))()`)

    if (!dom.bridge) problems.push('window.camAPI.saveSnapshot 未暴露（preload 桥接失败）')
    if (dom.resolutions < 2) problems.push(`分辨率选项未填充（${dom.resolutions}）`)
    if (dom.toggleLabel !== '开启摄像头') problems.push(`按钮初始文案异常：${dom.toggleLabel}`)
    if (!dom.placeholderVisible) problems.push('初始占位提示未显示')

    // 真开一次摄像头；授权后再枚举设备，验证「授权后重刷设备列表」这条路径
    const cam = await win.webContents.executeJavaScript(`(async () => {
      try {
        const s = await navigator.mediaDevices.getUserMedia({ video: { width: { ideal: 1280 } }, audio: false })
        const t = s.getVideoTracks()[0]
        const st = t.getSettings()
        const cams = (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === 'videoinput')
        const out = { ok: true, label: t.label, width: st.width, height: st.height, camCount: cams.length, named: cams.every(c => c.label) }
        s.getTracks().forEach(x => x.stop())
        return out
      } catch (e) { return { ok: false, name: e.name, message: e.message } }
    })()`)

    if (cam.ok) {
      console.log(`  camera      : ${cam.label || '(未命名)'} ${cam.width}x${cam.height}`)
      console.log(`  cameras     : ${cam.camCount} 个（${cam.named ? '名称已显示' : '名称为空'}）`)
      if (cam.camCount === 0) problems.push('取流成功但枚举不到设备')
      if (!cam.named) problems.push('授权后设备名仍为空（应能拿到真实名称）')
    } else if (cam.name === 'NotFoundError') {
      warnings.push('本机未检测到摄像头，取流能力未实测')
    } else {
      problems.push(`getUserMedia 失败 ${cam.name}: ${cam.message}`)
    }

    console.log(`  bridge      : ${dom.bridge ? 'OK' : 'FAIL'}`)
    console.log(`  resolutions : ${dom.resolutions}`)

    // 拍照保存：绕开需要人点确认的保存对话框，直接打真实写盘函数
    await checkSnapshotWrite(win)

    // 视觉链路：真实抽帧 → IPC → Python → 结果回渲染进程
    await checkVision(win)
  } catch (error) {
    problems.push(`加载界面异常: ${error.message}`)
  }

  console.log('')
  warnings.forEach((w) => console.log(`WARN  ${w}`))
  problems.forEach((p) => console.log(`FAIL  ${p}`))
  console.log(problems.length === 0 ? '\nSMOKE PASS' : `\nSMOKE FAIL (${problems.length})`)

  app.exit(problems.length === 0 ? 0 : 1)
})
