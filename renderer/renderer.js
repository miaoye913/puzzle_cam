const el = {
  video: document.getElementById('video'),
  placeholder: document.getElementById('placeholder'),
  toggle: document.getElementById('toggle'),
  snapshot: document.getElementById('snapshot'),
  rescan: document.getElementById('rescan'),
  device: document.getElementById('device'),
  resolution: document.getElementById('resolution'),
  status: document.getElementById('status'),
  stats: document.getElementById('stats'),
  recognize: document.getElementById('recognize'),
  visionScale: document.getElementById('vision-scale'),
  visionOverlay: document.getElementById('vision-overlay'),
  visionStats: document.getElementById('vision-stats'),
  overlay: document.getElementById('overlay'),
  board: document.getElementById('board'),
  assembly: document.getElementById('assembly'),
  assemblyHint: document.getElementById('assembly-hint'),
  visionLog: document.getElementById('vision-log'),
}

const VISION_JPEG_QUALITY = 0.8

/**
 * 摄像头是架在板子对面的，拍出来的画面左右和实物相反（实物靠右的拍出来在左），
 * 所以要翻一次才是你眼里看到的方向。
 *
 * 这一处**同时决定三件事**，改一个地方就全跟着变：
 *   - 抽帧：送进 Python 的帧先翻过来（所以板面图、碎片坐标、拼接结果全都在「你的方向」上）
 *   - 右下角预览小窗：也翻过来，和主画面方向一致
 *   - 拍照保存：存下来的图也是你看到的方向
 * 哪天摄像头挪到和人在同一侧了，把这里改成 false 就行。
 */
const CAMERA_FLIP_X = true

const RESOLUTIONS = [
  { label: '默认', value: '' },
  { label: '640 × 480', value: '640x480' },
  { label: '1280 × 720 (720p)', value: '1280x720' },
  { label: '1920 × 1080 (1080p)', value: '1920x1080' },
  { label: '2560 × 1440 (2K)', value: '2560x1440' },
  { label: '3840 × 2160 (4K)', value: '3840x2160' },
]

/** @type {MediaStream | null} */
let stream = null
let frameCounter = 0
let lastFpsAt = 0

function setStatus(text, isError = false) {
  el.status.textContent = text
  el.status.style.color = isError ? '#ff8f8f' : ''
}

function showPlaceholder(title, hint, isError = false) {
  el.placeholder.classList.remove('hidden')
  el.placeholder.classList.toggle('error', isError)
  el.placeholder.replaceChildren()

  const heading = document.createElement('p')
  heading.textContent = title
  el.placeholder.append(heading)

  if (hint) {
    const detail = document.createElement('p')
    detail.className = 'hint'
    detail.textContent = hint
    el.placeholder.append(detail)
  }
}

function hidePlaceholder() {
  el.placeholder.classList.add('hidden')
}

function currentConstraints() {
  const [width, height] = (el.resolution.value || '').split('x').map(Number)
  const video = el.device.value ? { deviceId: { exact: el.device.value } } : {}
  if (width && height) Object.assign(video, { width: { ideal: width }, height: { ideal: height } })
  return { video, audio: false }
}

/** 枚举摄像头；没有权限时 label 为空，用编号兜底 */
async function refreshDevices(selectId) {
  const devices = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === 'videoinput')
  const keep = selectId ?? el.device.value
  el.device.innerHTML = ''

  devices.forEach((device, index) => {
    const option = document.createElement('option')
    option.value = device.deviceId
    option.textContent = device.label || `摄像头 ${index + 1}`
    el.device.append(option)
  })

  if (keep && devices.some((d) => d.deviceId === keep)) el.device.value = keep
  el.device.disabled = devices.length <= 1
  return devices
}

async function start() {
  if (stream) return
  el.toggle.disabled = true
  setStatus('正在打开摄像头…')

  try {
    stream = await navigator.mediaDevices.getUserMedia(currentConstraints())
    el.video.srcObject = stream
    await el.video.play()

    hidePlaceholder()
    el.video.classList.remove('hidden')
    el.toggle.textContent = '关闭摄像头'
    el.toggle.classList.add('stop')
    el.snapshot.disabled = false
    el.recognize.disabled = false
    syncOverlayVisibility()

    // 授权之后才能拿到设备名称，此时再刷新一次列表
    await refreshDevices()

    const track = stream.getVideoTracks()[0]
    const settings = track.getSettings()
    setStatus(`预览中 · ${track.label || '摄像头'} · ${settings.width}×${settings.height}`)
  } catch (error) {
    stream = null
    const message =
      error.name === 'NotAllowedError'
        ? '摄像头权限被拒绝。请检查 Windows 设置 → 隐私和安全性 → 相机，允许桌面应用访问相机。'
        : error.name === 'NotFoundError'
          ? '未检测到摄像头设备。'
          : error.name === 'NotReadableError'
            ? '摄像头被其他程序占用，请关闭后重试。'
            : `${error.name}: ${error.message}`
    showPlaceholder('无法开启摄像头', message, true)
    setStatus('开启失败', true)
    el.snapshot.disabled = true
  } finally {
    el.toggle.disabled = false
  }
}

function stop() {
  if (visionOn) stopVision()
  el.recognize.disabled = true

  stream?.getTracks().forEach((track) => track.stop())
  stream = null
  el.video.srcObject = null
  el.video.classList.add('hidden')
  el.toggle.textContent = '开启摄像头'
  el.toggle.classList.remove('stop')
  el.snapshot.disabled = true
  el.stats.textContent = ''
  showPlaceholder('摄像头已关闭', '点击「开启摄像头」重新开始预览')
  setStatus('已关闭')
}

/** 切换设备/分辨率：保持开关状态，重建数据流 */
async function restart() {
  if (!stream) return
  stop()
  await start()
}

async function takeSnapshot() {
  if (!stream) return
  const { videoWidth, videoHeight } = el.video
  if (!videoWidth || !videoHeight) return

  const canvas = document.createElement('canvas')
  canvas.width = videoWidth
  canvas.height = videoHeight
  const ctx = canvas.getContext('2d')
  if (CAMERA_FLIP_X) {
    ctx.translate(videoWidth, 0)
    ctx.scale(-1, 1)
  }
  ctx.drawImage(el.video, 0, 0, videoWidth, videoHeight)

  const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19)
  try {
    const result = await window.camAPI.saveSnapshot(canvas.toDataURL('image/png'), `snapshot-${stamp}.png`)
    if (result.canceled) return
    if (!result.ok) throw new Error(result.error ?? '写入失败')
    setStatus(`已保存：${result.filePath}`)
  } catch (error) {
    setStatus(`保存失败：${error.message}`, true)
  }
}

/**
 * 统计摄像头真实帧率。
 * 用 requestVideoFrameCallback 而不是 requestAnimationFrame：后者数的是屏幕刷新率
 * （在 165Hz 屏上会恒定显示 165），跟摄像头实际出帧速度无关，会误导人。
 */
function onVideoFrame(now) {
  el.video.requestVideoFrameCallback(onVideoFrame)

  if (!stream) {
    frameCounter = 0
    lastFpsAt = 0
    return
  }
  if (lastFpsAt === 0) {
    lastFpsAt = now
    frameCounter = 0
    return
  }

  frameCounter += 1
  const elapsed = now - lastFpsAt
  if (elapsed < 1000) return

  const settings = stream.getVideoTracks()[0].getSettings()
  el.stats.textContent = `${settings.width}×${settings.height} · ${Math.round((frameCounter * 1000) / elapsed)} FPS`
  frameCounter = 0
  lastFpsAt = now
}

// ── OpenCV 视觉分析 ──────────────────────────────────────────────────

let visionOn = false
let visionBusy = false
let visionFrames = 0
let visionFpsAt = 0
let lastResult = null

function appendVisionLog(line) {
  el.visionLog.textContent += `${line}\n`
  // 只保留末尾若干行，长时间运行不至于把内存堆满
  const lines = el.visionLog.textContent.split('\n')
  if (lines.length > 300) el.visionLog.textContent = lines.slice(-200).join('\n')
  el.visionLog.scrollTop = el.visionLog.scrollHeight
}

/**
 * 抓当前视频帧，压成 JPEG 交给 Python。
 *
 * 要注意宽高比：目标分辨率固定 16:9，摄像头若是 4:3，直接 drawImage 会被拉伸，
 * 角度和长宽比全部失真。所以按目标的宽高比做中心裁剪，并把裁剪参数一并带回去，
 * 让叠加层能把结果坐标反映射回画面位置。
 */
async function captureForVision() {
  const [targetW, targetH] = el.visionScale.value.split('x').map(Number)
  const vw = el.video.videoWidth
  const vh = el.video.videoHeight
  if (!vw || !vh) return null

  const targetAspect = targetW / targetH
  let sw = vw
  let sh = vh
  if (vw / vh > targetAspect) sw = Math.round(vh * targetAspect)
  else sh = Math.round(vw / targetAspect)
  const sx = Math.round((vw - sw) / 2)
  const sy = Math.round((vh - sh) / 2)

  const canvas = document.createElement('canvas')
  canvas.width = targetW
  canvas.height = targetH
  const ctx = canvas.getContext('2d')
  if (CAMERA_FLIP_X) {
    // 见 CAMERA_FLIP_X 的说明：先翻正，后面 Python 算出来的一切才都在「你的方向」上
    ctx.translate(targetW, 0)
    ctx.scale(-1, 1)
  }
  ctx.drawImage(el.video, sx, sy, sw, sh, 0, 0, targetW, targetH)

  const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', VISION_JPEG_QUALITY))
  if (!blob) return null
  return {
    bytes: new Uint8Array(await blob.arrayBuffer()),
    mapping: { sx, sy, sw, sh, frameW: targetW, frameH: targetH },
  }
}

/**
 * 板面画布的位置和缩放。
 *
 * 和一个横平竖直的板面打交道，比原来省事得多：原来的主画面是摄像头原始画面，
 * 坐标要在「分析坐标 → 视频像素 → 屏幕像素」之间倒腾，还要处理镜像；
 * 现在主画面**就是**矫正后的板面，素材坐标和显示坐标只差一个缩放。
 */
function fitBoard(boardW, boardH, availW, availH) {
  const scale = Math.min(availW / Math.max(boardW, 1), availH / Math.max(boardH, 1))
  const w = Math.max(1, Math.round(boardW * scale))
  const h = Math.max(1, Math.round(boardH * scale))
  return {
    w,
    h,
    scale,
    left: Math.round((availW - w) / 2),
    top: Math.round((availH - h) / 2),
  }
}

function boardLayout(boardW, boardH) {
  const pane = el.board.parentElement.getBoundingClientRect()
  const pad = 12
  return fitBoard(
    boardW,
    boardH,
    Math.max(40, pane.width - pad * 2),
    Math.max(40, pane.height - pad * 2),
  )
}

/** 把 canvas 摆到指定位置尺寸并清空，返回已经按 DPR 设好变换的 2D 上下文 */
function placeCanvas(canvas, w, h, left, top) {
  const dpr = window.devicePixelRatio || 1
  canvas.width = Math.round(w * dpr)
  canvas.height = Math.round(h * dpr)
  canvas.style.width = `${w}px`
  canvas.style.height = `${h}px`
  canvas.style.left = `${left}px`
  canvas.style.top = `${top}px`
  const ctx = canvas.getContext('2d')
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  ctx.clearRect(0, 0, w, h)
  return ctx
}

let boardImage = null
let boardImageData = null

/** 主画面：矫正后的板面。没有板面就返回 null，调用方据此跳过叠加层 */
function drawBoard(result) {
  const rectSize = result?.board?.rect_size
  if (!result?.board?.found || !rectSize) return null
  const layout = boardLayout(rectSize[0], rectSize[1])
  const ctx = placeCanvas(el.board, layout.w, layout.h, layout.left, layout.top)
  if (boardImage && boardImage.complete && boardImage.naturalWidth) {
    ctx.drawImage(boardImage, 0, 0, layout.w, layout.h)
  }
  return layout
}

/** 板面图是 base64 来的，解码是异步的，解好了再重画一次 */
function setBoardImage(encoded) {
  if (!encoded || encoded === boardImageData) return
  boardImageData = encoded
  const img = new Image()
  img.onload = () => {
    boardImage = img
    renderView()
  }
  img.src = `data:image/jpeg;base64,${encoded}`
}

function renderView() {
  const layout = drawBoard(lastResult)
  drawOverlay(lastResult, layout)
  drawAssembly(lastResult)
}

/**
 * 挑一条线当左右分界线：要近竖直、要长。
 * 一条都没有就退回板子的水平中线——不能因为没检测到线就不分区、开天窗。
 */
function pickDivider(result) {
  const boardW = result?.board?.rect_size?.[0] ?? 1
  let best = null
  for (const line of result?.lines ?? []) {
    if (!line.p1_warp || !line.p2_warp) continue
    const offVertical = Math.abs(line.angle - 90)
    if (offVertical > 25) continue
    if (!best || line.length > best.length) best = line
  }
  if (best) {
    return { x: (best.p1_warp[0] + best.p2_warp[0]) / 2, fromLine: true, lineId: best.id }
  }
  return { x: boardW / 2, fromLine: false, lineId: null }
}

/**
 * 拼好的矩形摆在左半区的哪儿，以及它和板面坐标怎么换算。
 *
 * 按**和碎片一样的比例**摆（1 板面像素 = 1 板面像素），这样两边的块大小能直接对着比；
 * 只有放不下时才缩小。toBoard 是给路径规划用的——终点得落在**板面坐标**上，
 * 以后要真去驱动机器，用的就是这套坐标。
 */
function makeAssemblyMap(assembly, layout, zoneW, zoneH) {
  if (!assembly?.placements?.length || !assembly.size) return null
  const [aw, ah] = assembly.size
  const margin = 18
  const fit = Math.min(
    layout.scale,                                     // 和碎片同比例
    Math.max(1, zoneW - margin * 2) / Math.max(aw, 1), // 放不下才缩
    Math.max(1, zoneH - margin * 2) / Math.max(ah, 1),
  )
  const w = aw * fit
  const h = ah * fit
  const ox = (zoneW - w) / 2
  const oy = Math.max(margin + 12, (zoneH - h) / 2)
  return {
    fit,
    ox,
    oy,
    // 拼装坐标 → 画布像素
    toCanvas: (x, y) => [ox + x * fit, oy + y * fit],
    // 拼装坐标 → 板面坐标
    toBoard: (x, y) => [(ox + x * fit) / layout.scale, (oy + y * fit) / layout.scale],
  }
}

/** 多边形的重心（顶点平均，够用了；这里只要一个稳定的代表点） */
function polyCentroid(poly) {
  return poly.reduce((acc, p) => [acc[0] + p[0] / poly.length, acc[1] + p[1] / poly.length], [0, 0])
}

/**
 * 路径规划：每块碎片从「现在在哪」到「拼好后该在哪」。
 *
 * 两块的位置都用**板面坐标**表示，所以这里算出来的位移和转角是可以直接交给机器去执行的
 * （现在就差一个「板面像素 ↔ 机器脉冲」的换算，那套标定还没做）。
 *
 * 返回 [{ id, from, to, dx, dy, dist, dRot }]，from/to 是重心。
 */
function planPaths(result, assemblyMap) {
  if (!assemblyMap || !result?.assembly?.placements) return []
  const paths = []
  for (const item of result.assembly.placements) {
    const piece = result.pieces.find((p) => p.id === item.id)
    if (!piece?.polygon_warp || item.polygon.length < 2) continue
    const targetBoard = item.polygon.map(([x, y]) => assemblyMap.toBoard(x, y))
    const from = polyCentroid(piece.polygon_warp)
    const to = polyCentroid(targetBoard)
    // 转角**直接用侧车给的**——它是从求解时实际施加的那个旋转矩阵算出来的，一定对。
    //
    // 曾经在这里拿「碎片第 0 条边」和「目标第 0 条边」的方向相减，那是错的：
    // 侧车拼接前会把碎块统一转成同一个绕向，而识别出来的轮廓是另一个绕向，
    // 两边的「第 0 条边」根本不是同一条边——算出来能差上百度（实测 4 块里错了 3 块）。
    const dRot = item.rot_deg ?? 0
    paths.push({
      id: item.id,
      from,
      to,
      dx: to[0] - from[0],
      dy: to[1] - from[1],
      dist: Math.hypot(to[0] - from[0], to[1] - from[1]),
      dRot,
      targetPolygon: targetBoard,
    })
  }
  return paths
}

/**
 * 板面坐标网格：**刻度标的是毫米**。
 *
 * 标毫米而不是脉冲或像素，是因为毫米才是那个「物理上说得通」的量纲——像素绑死分辨率，
 * 脉冲绑死电机，只有毫米既是板子上的真实位置、又能和机器对话（乘一个脉冲/毫米就是脉冲）。
 * 方向翻转由 boardPxToMm 自己处理，这里不用管。
 */
function drawCalibrationGrid(ctx, rectW, rectH, layout) {
  const divisions = 8
  const GRID = 'rgba(255,255,255,0.10)'
  const TEXT = 'rgba(255,255,255,0.6)'
  const HALO = 'rgba(0,0,0,0.7)'
  ctx.save()
  ctx.font = '500 10px "Segoe UI", "Microsoft YaHei", sans-serif'

  const tick = (text, x, y, align) => {
    ctx.textAlign = align
    ctx.lineWidth = 3
    ctx.strokeStyle = HALO
    ctx.strokeText(text, x, y)
    ctx.fillStyle = TEXT
    ctx.fillText(text, x, y)
  }

  for (let i = 1; i < divisions; i++) {
    const gx = (i / divisions) * rectW * layout.scale
    ctx.strokeStyle = GRID
    ctx.lineWidth = 1
    ctx.beginPath()
    ctx.moveTo(gx, 0)
    ctx.lineTo(gx, layout.h)
    ctx.stroke()
    tick(String(Math.round(boardPxToMm((i / divisions) * rectW, 0, rectW, rectH)[0])),
      gx, layout.h - 5, 'center')
  }
  for (let i = 1; i < divisions; i++) {
    const gy = (i / divisions) * rectH * layout.scale
    ctx.strokeStyle = GRID
    ctx.lineWidth = 1
    ctx.beginPath()
    ctx.moveTo(0, gy)
    ctx.lineTo(layout.w, gy)
    ctx.stroke()
    tick(String(Math.round(boardPxToMm(0, (i / divisions) * rectH, rectW, rectH)[1])),
      5, gy - 3, 'left')
  }
  ctx.restore()
}

/** 在板面上点出来的目标点（板面像素）。点完画个标记，机器走过去之后两者就重合了 */
let clickTarget = null

/** 点选的目标：一个准心 + 从机器当前位置连过来的虚线（机器在走，线会一直缩短） */
function drawClickTarget(ctx, rectW, rectH, layout) {
  if (!clickTarget) return
  const cx = clickTarget[0] * layout.scale
  const cy = clickTarget[1] * layout.scale
  ctx.save()
  ctx.strokeStyle = PATH_COLOR
  ctx.lineWidth = 2
  ctx.beginPath()
  ctx.arc(cx, cy, 9, 0, Math.PI * 2)
  ctx.stroke()
  ctx.beginPath()
  ctx.moveTo(cx - 15, cy)
  ctx.lineTo(cx - 5, cy)
  ctx.moveTo(cx + 5, cy)
  ctx.lineTo(cx + 15, cy)
  ctx.moveTo(cx, cy - 15)
  ctx.lineTo(cx, cy - 5)
  ctx.moveTo(cx, cy + 5)
  ctx.lineTo(cx, cy + 15)
  ctx.stroke()

  if (machinePose) {
    const [mx, my] = pulsesToBoard(machinePose.x, machinePose.y, rectW, rectH)
    ctx.setLineDash([5, 4])
    ctx.strokeStyle = 'rgba(255,209,102,0.75)'
    ctx.lineWidth = 1.5
    ctx.beginPath()
    ctx.moveTo(mx * layout.scale, my * layout.scale)
    ctx.lineTo(cx, cy)
    ctx.stroke()
  }
  ctx.restore()
}

/** 机器此刻在板子的哪儿。脉冲跑出标定范围时画成红色、提示超出 */
function drawMachineMarker(ctx, rectW, rectH, layout) {
  if (!machinePose) return
  const [bx, by] = pulsesToBoard(machinePose.x, machinePose.y, rectW, rectH)
  const cx = bx * layout.scale
  const cy = by * layout.scale
  const out = pulseOutOfRange(machinePose.x, machinePose.y)
  const color = out ? '#ff8a8a' : '#5ef1c0'

  ctx.save()
  ctx.strokeStyle = color
  ctx.lineWidth = 1.5
  ctx.beginPath()
  ctx.moveTo(cx - 14, cy)
  ctx.lineTo(cx + 14, cy)
  ctx.moveTo(cx, cy - 14)
  ctx.lineTo(cx, cy + 14)
  ctx.stroke()
  ctx.beginPath()
  ctx.arc(cx, cy, 6, 0, Math.PI * 2)
  ctx.stroke()

  const [xmm, ymm] = pulsesToMm(machinePose.x, machinePose.y)
  const text = `脉冲(${Math.round(machinePose.x)}, ${Math.round(machinePose.y)}) → ` +
    `板面(${xmm.toFixed(0)}, ${ymm.toFixed(0)}) mm${out ? ' 超出范围' : ''}`
  ctx.font = '600 12px "Segoe UI", "Microsoft YaHei", sans-serif'
  ctx.textAlign = 'center'
  ctx.lineWidth = 3
  ctx.strokeStyle = 'rgba(0,0,0,0.8)'
  ctx.strokeText(text, cx, cy - 20)
  ctx.fillStyle = color
  ctx.fillText(text, cx, cy - 20)
  ctx.restore()
}

function drawOverlay(result, layout) {
  const canvas = el.overlay
  if (!result || !layout) {
    canvas.classList.add('hidden')
    return
  }
  const ctx = placeCanvas(canvas, layout.w, layout.h, layout.left, layout.top)
  canvas.classList.remove('hidden')
  ctx.lineJoin = 'round'

  const boardW = result.board.rect_size[0]
  const boardH = result.board.rect_size[1]
  const displayW = layout.w
  const displayH = layout.h

  // 矫正坐标 → 画布像素
  const project = (x, y) => [x * layout.scale, y * layout.scale]

  // ── 分界线：左拼图区 / 右识别区 ──────────────────────────────
  const divider = pickDivider(result)
  const dx = divider.x * layout.scale
  ctx.fillStyle = 'rgba(76, 141, 255, 0.06)'
  ctx.fillRect(0, 0, dx, displayH)
  ctx.fillStyle = 'rgba(61, 220, 151, 0.06)'
  ctx.fillRect(dx, 0, displayW - dx, displayH)
  ctx.save()
  ctx.setLineDash([7, 5])
  ctx.strokeStyle = 'rgba(255,255,255,0.65)'
  ctx.lineWidth = 1.5
  ctx.beginPath()
  ctx.moveTo(dx, 0)
  ctx.lineTo(dx, displayH)
  ctx.stroke()
  ctx.restore()

  const zoneLabel = (text, cx, color) => {
    ctx.font = '600 13px "Segoe UI", "Microsoft YaHei", sans-serif'
    ctx.textAlign = 'center'
    ctx.lineWidth = 3
    ctx.strokeStyle = 'rgba(0,0,0,0.75)'
    ctx.strokeText(text, cx, 20)
    ctx.fillStyle = color
    ctx.fillText(text, cx, 20)
    ctx.textAlign = 'start'
  }
  zoneLabel('拼图区 · 拼好的矩形', dx / 2, '#9dc0ff')
  zoneLabel('识别区 · 识别到的碎片', dx + (displayW - dx) / 2, '#9df0c4')

  // 板面坐标网格：刻度是脉冲，方便对着标定读数
  drawCalibrationGrid(ctx, boardW, boardH, layout)

  // ── 左半「拼图区」：拼好的矩形 ────────────────────────────────
  // 按**和碎片一样的比例**画（1 板面像素 = 1 板面像素），这样两边的块大小能直接对着比；
  // 只有放不下时才缩小。左下角那个浮动小窗里另有一份放大过的，看细节用那个。
  const assembly = result.assembly
  const assemblyMap = makeAssemblyMap(assembly, layout, dx, displayH)
  ctx.save()
  ctx.beginPath()
  ctx.rect(0, 0, dx, displayH)
  ctx.clip()
  if (assemblyMap) {
    assembly.placements.forEach((item, index) => {
      ctx.beginPath()
      item.polygon.forEach(([x, y], i) => {
        const [px, py] = assemblyMap.toCanvas(x, y)
        if (i) ctx.lineTo(px, py)
        else ctx.moveTo(px, py)
      })
      ctx.closePath()
      const color = ASSEMBLY_COLORS[index % ASSEMBLY_COLORS.length]
      ctx.fillStyle = `${color}55`
      ctx.fill()
      ctx.strokeStyle = color
      ctx.lineWidth = 2
      ctx.stroke()
    })
    ctx.font = '600 12px "Segoe UI", "Microsoft YaHei", sans-serif'
    ctx.fillStyle = '#8b9bb4'
    ctx.fillText(`拼好 ${assembly.size[0].toFixed(0)}×${assembly.size[1].toFixed(0)}`,
      assemblyMap.ox, assemblyMap.oy - 8)
  } else {
    ctx.font = '600 13px "Segoe UI", "Microsoft YaHei", sans-serif'
    ctx.textAlign = 'center'
    ctx.fillStyle = '#8b929e'
    ctx.fillText('这几块没拼出矩形', dx / 2, displayH / 2)
    ctx.textAlign = 'start'
  }
  ctx.restore()

  // ── 右半「识别区」：识别到的碎片。按实际位置画，**伸进左半边的部分裁掉**，
  //    免得和拼图区里的矩形糊在一起 ──
  ctx.save()
  ctx.beginPath()
  ctx.rect(dx, 0, displayW - dx, displayH)
  ctx.clip()
  for (const piece of result.pieces) {
    const src = piece.polygon_warp ?? piece.polygon
    const poly = src.map(([x, y]) => project(x, y))
    ctx.beginPath()
    poly.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)))
    ctx.closePath()
    ctx.strokeStyle = '#ff3df0'
    ctx.lineWidth = 2
    ctx.stroke()

    const cx = src.reduce((sum, p) => sum + p[0], 0) / src.length
    const cy = src.reduce((sum, p) => sum + p[1], 0) / src.length
    const [sx, sy] = project(cx, cy)
    ctx.beginPath()
    ctx.arc(sx, sy, 3.5, 0, Math.PI * 2)
    ctx.fillStyle = '#3df0ff'
    ctx.fill()

    ctx.font = '600 13px "Segoe UI", "Microsoft YaHei", sans-serif'
    ctx.fillStyle = '#3df0ff'
    const label = `#${piece.id}`
    ctx.lineWidth = 3
    ctx.strokeStyle = 'rgba(0,0,0,0.75)'
    ctx.strokeText(label, sx + 8, sy + 4)
    ctx.fillText(label, sx + 8, sy + 4)
  }
  ctx.restore()   // 结束「识别区」的裁剪

  // ── 搬运路径：每块碎片从「现在在哪」连到「拼好后该在哪」 ──────────
  // 画在两个区的裁剪**之外**：线本来就要横跨分界线。
  // 只画线，不做任何运动——离真驱动还差一套「板面像素 ↔ 机器脉冲」的标定。
  const paths = planPaths(result, assemblyMap)
  if (paths.length) {
    ctx.save()
    ctx.lineWidth = 1.5
    ctx.strokeStyle = PATH_COLOR
    ctx.fillStyle = PATH_COLOR
    for (const path of paths) {
      const [x0, y0] = project(path.from[0], path.from[1])
      const [x1, y1] = project(path.to[0], path.to[1])

      // 目标位置画一圈虚线框：一眼看出这块要落到哪儿、转成什么姿态
      ctx.save()
      ctx.setLineDash([4, 4])
      ctx.globalAlpha = 0.75
      ctx.beginPath()
      path.targetPolygon.forEach(([bx, by], i) => {
        const [px, py] = project(bx, by)
        if (i) ctx.lineTo(px, py)
        else ctx.moveTo(px, py)
      })
      ctx.closePath()
      ctx.stroke()
      ctx.restore()

      // 起点那个小圈表示「从这儿搬走」
      ctx.beginPath()
      ctx.arc(x0, y0, 3, 0, Math.PI * 2)
      ctx.stroke()

      // 连线 + 箭头
      ctx.beginPath()
      ctx.moveTo(x0, y0)
      ctx.lineTo(x1, y1)
      ctx.stroke()
      const angle = Math.atan2(y1 - y0, x1 - x0)
      const head = 9
      ctx.beginPath()
      ctx.moveTo(x1, y1)
      ctx.lineTo(x1 - head * Math.cos(angle - 0.4), y1 - head * Math.sin(angle - 0.4))
      ctx.lineTo(x1 - head * Math.cos(angle + 0.4), y1 - head * Math.sin(angle + 0.4))
      ctx.closePath()
      ctx.fill()

      // 标一下这块要转多少度——光看一条线看不出转角
      const mx = (x0 + x1) / 2
      const my = (y0 + y1) / 2
      const text = `#${path.id}  ${path.dRot >= 0 ? '+' : ''}${path.dRot.toFixed(0)}°`
      ctx.font = '600 12px "Segoe UI", "Microsoft YaHei", sans-serif'
      ctx.lineWidth = 3
      ctx.strokeStyle = 'rgba(0,0,0,0.8)'
      ctx.strokeText(text, mx + 6, my - 4)
      ctx.fillStyle = PATH_COLOR
      ctx.fillText(text, mx + 6, my - 4)
      ctx.lineWidth = 1.5
      ctx.strokeStyle = PATH_COLOR
    }
    ctx.restore()
  }

  // ── 板面上的线条：按**实测颜色**画（红线、记号笔、拼缝都适用）。
  // 当分界线用的那条已经画成分区线了，这里只标一下它的颜色，不重复描一遍。
  for (const line of result.lines ?? []) {
    if (!line.p1_warp || !line.p2_warp) continue
    const [x1, y1] = project(line.p1_warp[0], line.p1_warp[1])
    const [x2, y2] = project(line.p2_warp[0], line.p2_warp[1])
    const isDivider = divider.lineId === line.id
    if (!isDivider) {
      const [r, g, b] = line.color.rgb
      ctx.beginPath()
      ctx.moveTo(x1, y1)
      ctx.lineTo(x2, y2)
      ctx.lineWidth = 5
      ctx.strokeStyle = 'rgba(0,0,0,0.6)' // 先描一圈深色，免得线色和背景糊在一起
      ctx.stroke()
      ctx.lineWidth = 3
      ctx.strokeStyle = `rgb(${r},${g},${b})`
      ctx.stroke()
    }

    // 标签往线的法线方向让开，免得两条线交叉时两个标签叠在一起
    const len = Math.hypot(x2 - x1, y2 - y1) || 1
    const nx = -(y2 - y1) / len
    const ny = (x2 - x1) / len
    const mx = (x1 + x2) / 2 + nx * 12
    const my = (y1 + y2) / 2 + ny * 12
    const text = `线${line.id} ${line.length.toFixed(0)}px${line.red ? ' 红' : ''}`
    ctx.font = '600 12px "Segoe UI", "Microsoft YaHei", sans-serif'
    ctx.fillStyle = line.red ? '#ff5252' : '#ffffff'
    ctx.lineWidth = 3
    ctx.strokeStyle = 'rgba(0,0,0,0.75)'
    ctx.strokeText(text, mx, my)
    ctx.fillText(text, mx, my)
  }

  // 机器位置和目标点画在最上层：它们是「此刻在哪 / 要去哪」，被别的东西盖住就没意义了
  drawMachineMarker(ctx, boardW, boardH, layout)
  drawClickTarget(ctx, boardW, boardH, layout)

  // 画完再按「显示叠加层」那个勾决定露不露脸，免得把用户的勾选覆盖掉
  syncOverlayVisibility()
}

// 拼好的矩形里每块碎片的配色。拼图区那张大图和左下角的小窗共用同一套，
// 同一块在两处颜色一致，对照着看不会串
const ASSEMBLY_COLORS = ['#4c8dff', '#3ddc97', '#ffb454', '#ff6b9d', '#b98cff', '#5ad1e6']

// 搬运路径的颜色。特意选个暖色，和碎片的洋红、拼好的矩形的蓝/绿/橙区分开
const PATH_COLOR = '#ffd166'

function visionStatsText(result) {
  const now = performance.now()
  visionFrames += 1
  if (visionFpsAt === 0) {
    visionFpsAt = now
    visionFrames = 0
  } else if (now - visionFpsAt >= 1000) {
    result.fps = Math.round((visionFrames * 1000) / (now - visionFpsAt))
    visionFrames = 0
    visionFpsAt = now
  }

  const board = result.board.found ? '拼板已定位' : '未找到拼板'
  const parts = [board, `${result.pieces.length} 块`, `${result.timing_ms.toFixed(1)}ms`]
  const redCount = (result.lines ?? []).filter((line) => line.red).length
  if (result.lines?.length) parts.push(redCount ? `${result.lines.length} 条线（红 ${redCount}）` : `${result.lines.length} 条线`)
  if (result.fps) parts.push(`${result.fps}fps`)
  if (result.error) parts.push(`错误: ${result.error}`)
  return parts.join(' · ')
}

/**
 * 识别一次：抓当前这一帧、交给 Python 算完，然后把结果**定住**。
 *
 * 实物摆好之后就不会再动，一直抽帧分析纯属白烧 CPU，而且叠加层一直在闪，反而看不清。
 * 所以这里没有循环——点一次按钮算一帧，算完就停住；再点一次重来。
 * 拼接求解是这次调用里一并算完的（侧车在 analyze 里就把它算好了），不用再发请求。
 */
async function recognizeOnce() {
  if (visionBusy || el.video.readyState < 2) return
  visionBusy = true
  el.recognize.disabled = true
  el.visionStats.textContent = '正在识别…'
  el.assemblyHint.classList.add('hidden')
  try {
    if (!visionOn) {
      const started = await window.camAPI.vision.start()
      if (!started) {
        el.visionStats.textContent = '没找到可用的 Python + OpenCV'
        return
      }
      visionOn = true
    }
    const captured = await captureForVision()
    if (!captured) {
      el.visionStats.textContent = '抓帧失败，确认摄像头画面正常'
      return
    }
    const reply = await window.camAPI.vision.analyze(captured.bytes)
    if (!reply.ok) {
      el.visionStats.textContent = `分析失败：${reply.error}`
      return
    }
    lastResult = reply.result
    el.visionStats.textContent = visionStatsText(reply.result)
    // 板面图是 base64，解码要等一下；解好了 setBoardImage 自己会再调一次 renderView
    setBoardImage(reply.result.board_image_b64)
    boardImage = null
    renderView()
  } catch (error) {
    el.visionStats.textContent = `识别异常：${error.message}`
  } finally {
    visionBusy = false
    el.recognize.disabled = false
    syncOverlayVisibility()
  }
}

function syncOverlayVisibility() {
  // 只要手上有一份「找到板子」的结果就该显示——不再看 visionOn：识别是一次性的，
  // 算完之后侧车还在跑不代表画面该不该显示
  const visible = el.visionOverlay.checked && (lastResult?.board?.found ?? false)
  el.overlay.classList.toggle('hidden', !visible)
}

function stopVision() {
  visionOn = false
  lastResult = null
  boardImage = null
  boardImageData = null
  el.visionStats.textContent = ''
  window.camAPI.vision.stop()
  el.board.getContext('2d').clearRect(0, 0, el.board.width, el.board.height)
  el.overlay.classList.add('hidden')
  clearAssembly()
}

/**
 * 左下小窗：把碎片按拼好的位置画出来。
 * 侧车只在真的拼出矩形时才会给出 placements，否则这里明确说没拼出来——
 * 不会拿一个「看着像矩形」的结果糊弄过去。
 */
function drawAssembly(result) {
  const canvas = el.assembly
  const box = canvas.parentElement.getBoundingClientRect()
  const dpr = window.devicePixelRatio || 1
  const pad = 16
  const availW = Math.max(40, box.width - pad * 2)
  const availH = Math.max(40, box.height - pad * 2)
  canvas.width = Math.round(availW * dpr)
  canvas.height = Math.round(availH * dpr)
  canvas.style.width = `${availW}px`
  canvas.style.height = `${availH}px`
  const ctx = canvas.getContext('2d')
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  ctx.clearRect(0, 0, availW, availH)

  const assembly = result?.assembly
  if (!assembly || !assembly.placements?.length) {
    el.assemblyHint.textContent = result?.board?.found
      ? '这几块没拼出矩形'
      : '没找到拼板，无法拼接'
    el.assemblyHint.classList.remove('hidden')
    return
  }
  el.assemblyHint.classList.add('hidden')

  const [rw, rh] = assembly.size
  const scale = Math.min(availW / rw, availH / rh)
  const ox = (availW - rw * scale) / 2
  const oy = (availH - rh * scale) / 2

  const colors = ASSEMBLY_COLORS
  assembly.placements.forEach((item, index) => {
    const poly = item.polygon
    ctx.beginPath()
    poly.forEach(([x, y], i) => {
      const px = ox + x * scale
      const py = oy + y * scale
      if (i) ctx.lineTo(px, py)
      else ctx.moveTo(px, py)
    })
    ctx.closePath()
    const color = colors[index % colors.length]
    ctx.fillStyle = `${color}44`
    ctx.fill()
    ctx.strokeStyle = color
    ctx.lineWidth = 2
    ctx.lineJoin = 'round'
    ctx.stroke()

    const c = poly.reduce((acc, p) => [acc[0] + p[0] / poly.length, acc[1] + p[1] / poly.length], [0, 0])
    ctx.font = '600 13px "Segoe UI", "Microsoft YaHei", sans-serif'
    ctx.fillStyle = color
    ctx.textAlign = 'center'
    ctx.fillText(`#${item.id}`, ox + c[0] * scale, oy + c[1] * scale + 4)
    ctx.textAlign = 'start'
  })

  ctx.font = '600 12px "Segoe UI", "Microsoft YaHei", sans-serif'
  ctx.fillStyle = '#8b929e'
  ctx.fillText(`拼成 ${rw.toFixed(0)} × ${rh.toFixed(0)}`, 4, availH - 4)
}

function clearAssembly() {
  const canvas = el.assembly
  const ctx = canvas.getContext('2d')
  ctx.setTransform(1, 0, 0, 1, 0, 0)
  ctx.clearRect(0, 0, canvas.width, canvas.height)
  el.assemblyHint.textContent = '点「识别一次」，拼好的矩形会显示在这里'
  el.assemblyHint.classList.remove('hidden')
}

function initControls() {
  RESOLUTIONS.forEach(({ label, value }) => {
    const option = document.createElement('option')
    option.value = value
    option.textContent = label
    el.resolution.append(option)
  })
  el.resolution.value = '1280x720'

  el.toggle.addEventListener('click', () => (stream ? stop() : start()))
  el.snapshot.addEventListener('click', takeSnapshot)
  el.rescan.addEventListener('click', async () => {
    await refreshDevices()
    setStatus(`找到 ${el.device.options.length} 个摄像头设备`)
  })
  el.device.addEventListener('change', restart)
  el.resolution.addEventListener('change', restart)

  navigator.mediaDevices.addEventListener('devicechange', () => refreshDevices().catch(() => {}))

  el.recognize.addEventListener('click', recognizeOnce)
  el.visionOverlay.addEventListener('change', syncOverlayVisibility)
  // 窗口尺寸变了，板面、叠加层、拼接小窗都要按新的盒子重画
  window.addEventListener('resize', renderView)
  window.camAPI.vision.onLog(appendVisionLog)

  // 右下角预览小窗和主画面保持同一方向（见 CAMERA_FLIP_X）
  el.video.classList.toggle('mirrored', CAMERA_FLIP_X)
  // 注意：不能给 video 加 .hidden——抽帧要从它拿数据，而且右下那个小窗还要显示它
  clearAssembly()
}

if (!navigator.mediaDevices?.getUserMedia) {
  showPlaceholder('当前环境不支持摄像头采集', '需要 Electron 渲染进程提供 mediaDevices', true)
} else {
  initControls()
  refreshDevices().catch(() => {})
  // 回调自己续排下一帧；没有画面时它不会被调用，也就自然不占 CPU
  el.video.requestVideoFrameCallback(onVideoFrame)
}
