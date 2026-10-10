/**
 * 串口控制台：操控 STM32 拼图机。
 *
 * 单独一个文件，免得 renderer.js 越堆越大。这里只负责界面和命令，帧协议、串口、
 * 后台读线程全在 puzzle_host/ 那套 Python 里（见 host/bridge.py）。
 *
 * 一个容易误会的地方：设备回的 ACK 只代表「命令排队了」，**不代表动作做完**。
 * 所以旁边的实时状态是靠周期 GET ALL 刷出来的，不要拿 ACK 当完成。
 */
const hostEl = {
  toggle: document.getElementById('host-toggle'),
  panel: document.getElementById('host-panel'),
  close: document.getElementById('host-close'),
  port: document.getElementById('host-port'),
  portList: document.getElementById('host-port-list'),
  refresh: document.getElementById('host-refresh'),
  baud: document.getElementById('host-baud'),
  connect: document.getElementById('host-connect'),
  ping: document.getElementById('host-ping'),
  reinit: document.getElementById('host-reinit'),
  getAll: document.getElementById('host-getall'),
  poll: document.getElementById('host-poll'),
  x: document.getElementById('host-x'),
  y: document.getElementById('host-y'),
  rot: document.getElementById('host-rot'),
  servoBtn: document.getElementById('act-servo'),
  emBtn: document.getElementById('act-em'),
  send: document.getElementById('host-send'),
  echo: document.getElementById('host-echo'),
  log: document.getElementById('host-log'),
  calXMin: document.getElementById('cal-xmin'),
  calXMax: document.getElementById('cal-xmax'),
  calYMin: document.getElementById('cal-ymin'),
  calYMax: document.getElementById('cal-ymax'),
  calInvX: document.getElementById('cal-invx'),
  calInvY: document.getElementById('cal-invy'),
  calBW: document.getElementById('cal-bw'),
  calBH: document.getElementById('cal-bh'),
  calReset: document.getElementById('cal-reset'),
  calReadout: document.getElementById('cal-readout'),
  calMove: document.getElementById('cal-move'),
  state: {
    x: document.getElementById('host-sx'),
    y: document.getElementById('host-sy'),
    rot: document.getElementById('host-sr'),
    homing: document.getElementById('host-sh'),
    servo: document.getElementById('host-ss'),
    em: document.getElementById('host-se'),
  },
}

const POLL_INTERVAL_MS = 1000
let hostStarted = false
let hostConnected = false

function hostLog(text) {
  const line = String(text ?? '')
  if (!line.trim()) return
  hostEl.log.textContent += `${line}\n`
  // 只留末尾若干行，长时间开着不至于把内存堆满
  const lines = hostEl.log.textContent.split('\n')
  if (lines.length > 400) hostEl.log.textContent = lines.slice(-250).join('\n')
  hostEl.log.scrollTop = hostEl.log.scrollHeight
}

/** 发一条命令；失败/设备报错都写进日志，不让它静默 */
async function hostCommand(payload) {
  if (!hostStarted) {
    const ok = await window.camAPI.host.start()
    if (!ok) {
      hostLog('桥接起不来——多半是没找到 Python（要能导入 puzzle_host）')
      return null
    }
    hostStarted = true
  }
  let wrapper
  try {
    wrapper = await window.camAPI.host.command(payload)
  } catch (error) {
    hostLog(`发送失败：${error.message}`)
    return null
  }
  if (!wrapper.ok) {
    hostLog(`命令失败：${wrapper.error}`)
    return null
  }
  const reply = wrapper.reply ?? {}
  if (reply.ok === false && reply.error) hostLog(`设备报错：${reply.error}`)
  return reply
}

function setConnected(connected) {
  hostConnected = connected
  hostEl.connect.textContent = connected ? '断开' : '连接'
  hostEl.connect.classList.toggle('stop', connected)
  hostEl.connect.classList.toggle('primary', !connected)
  updateActuatorButtons()          // 没连上时两个开关按不动
}

function showStatus(status) {
  if (!status) return
  hostEl.state.x.textContent = status.posX
  hostEl.state.y.textContent = status.posY
  hostEl.state.rot.textContent = `${status.posRot}°`
  hostEl.state.homing.textContent = status.homing ? '是' : '否'
  hostEl.state.servo.textContent = status.servo ? '开' : '关'
  hostEl.state.em.textContent = status.electromagnet ? '吸合' : '释放'
  // 工具栏上那两个开关跟着机器实际状态走——比如机器自己复位了，按钮不该还显示「开」
  const next = { servo: !!status.servo, em: !!status.electromagnet }
  if (next.servo !== actuator.servo || next.em !== actuator.em) {
    Object.assign(actuator, next)
    updateActuatorButtons()
  }
}

async function refreshPorts() {
  const reply = await hostCommand({ cmd: 'ports' })
  if (!reply) return
  hostEl.portList.innerHTML = ''
  for (const { device, description } of reply.ports ?? []) {
    const option = document.createElement('option')
    option.value = device
    option.label = description ? `${device} — ${description}` : device
    hostEl.portList.append(option)
  }
  if (!hostEl.port.value && reply.ports?.length) hostEl.port.value = reply.ports[0].device
  hostLog(`找到 ${reply.ports?.length ?? 0} 个串口${reply.havePyserial ? '' : '（pyserial 不可用）'}`)
}

async function toggleConnection() {
  if (hostConnected) {
    await hostCommand({ cmd: 'close' })
    await hostCommand({ cmd: 'poll', interval_ms: 0 })
    hostEl.poll.checked = false
    setConnected(false)
    return
  }
  const target = hostEl.port.value.trim()
  if (!target) {
    hostLog('先填一个串口（或 tcp://主机:端口）')
    return
  }
  const reply = await hostCommand({ cmd: 'open', target, baud: Number(hostEl.baud.value) || 115200 })
  if (reply?.ok) {
    setConnected(true)
    await hostCommand({ cmd: 'get_all' })
  }
}

async function sendSettings() {
  const reply = await hostCommand({
    cmd: 'set',
    x: Number(hostEl.x.value) || 0,
    y: Number(hostEl.y.value) || 0,
    rot: Number(hostEl.rot.value) || 0,
  })
  if (reply?.ok) hostLog(`已下发：${(reply.sent ?? []).join(' ')}`)
}

// ── 舵机 / 电磁铁：工具栏上的常驻开关 ───────────────────────────────
// 放在控制台外面，是因为这两个是操作过程中要随手按的，不该先展开面板才能碰
const actuator = { servo: false, em: false }

function updateActuatorButtons() {
  hostEl.servoBtn.classList.toggle('active', actuator.servo)
  hostEl.emBtn.classList.toggle('active', actuator.em)
  hostEl.servoBtn.textContent = actuator.servo ? '舵机 开' : '舵机 关'
  hostEl.emBtn.textContent = actuator.em ? '电磁铁 吸合' : '电磁铁 释放'
  const ready = hostConnected
  hostEl.servoBtn.disabled = !ready
  hostEl.emBtn.disabled = !ready
}

async function toggleActuator(which) {
  const next = !actuator[which]
  const before = actuator[which]
  actuator[which] = next          // 先按下去，手感跟手
  updateActuatorButtons()
  const reply = await hostCommand({ cmd: 'set', [which]: next ? 1 : 0 })
  if (!reply?.ok) {
    // 没发出去就翻回来，别让按钮显示一个机器根本没执行的状态
    actuator[which] = before
    updateActuatorButtons()
    return
  }
  hostLog(`${which === 'servo' ? '舵机' : '电磁铁'} → ${next ? '开' : '关'}`)
}

// ── 坐标映射（脉冲 ↔ 板面）────────────────────────────────────────
const CAL_DEFAULTS = {
  xMin: 500, xMax: 2200, yMin: 2200, yMax: 25100,
  invertX: false, invertY: false, boardW: 297, boardH: 210,
}

function fillCalibrationForm() {
  hostEl.calXMin.value = calibration.xMin
  hostEl.calXMax.value = calibration.xMax
  hostEl.calYMin.value = calibration.yMin
  hostEl.calYMax.value = calibration.yMax
  hostEl.calInvX.checked = calibration.invertX
  hostEl.calInvY.checked = calibration.invertY
  hostEl.calBW.value = calibration.boardW
  hostEl.calBH.value = calibration.boardH
}

function applyCalibrationForm() {
  const num = (input, fallback) => {
    const value = Number(input.value)
    return Number.isFinite(value) ? value : fallback
  }
  calibration.xMin = num(hostEl.calXMin, calibration.xMin)
  calibration.xMax = num(hostEl.calXMax, calibration.xMax)
  calibration.yMin = num(hostEl.calYMin, calibration.yMin)
  calibration.yMax = num(hostEl.calYMax, calibration.yMax)
  calibration.invertX = hostEl.calInvX.checked
  calibration.invertY = hostEl.calInvY.checked
  calibration.boardW = num(hostEl.calBW, calibration.boardW)
  calibration.boardH = num(hostEl.calBH, calibration.boardH)
  saveCalibration()
  renderView()          // 网格刻度和准星都跟着范围变，立刻重画
  updateCalReadout()
}

function updateCalReadout() {
  const rect = lastResult?.board?.rect_size
  if (!machinePose) {
    hostEl.calReadout.textContent = '机器位置：未连接'
    return
  }
  if (!rect) {
    hostEl.calReadout.textContent =
      `机器脉冲(${Math.round(machinePose.x)}, ${Math.round(machinePose.y)})，还没有板面`
    return
  }
  const [xmm, ymm] = pulsesToMm(machinePose.x, machinePose.y)
  const out = pulseOutOfRange(machinePose.x, machinePose.y)
  hostEl.calReadout.textContent =
    `机器脉冲(${Math.round(machinePose.x)}, ${Math.round(machinePose.y)}) → ` +
    `板面(${xmm.toFixed(0)}, ${ymm.toFixed(0)}) mm${out ? ' 超出范围' : ''}`
}

/** 机器位置一变就重画板面，准星跟着走 */
function onMachinePose(status) {
  machinePose = { x: status.posX, y: status.posY }
  updateCalReadout()
  renderView()
}

/**
 * 点板面 → 机器开过去。
 *
 * 换算链是：点击的画布像素 → 板面像素 → 毫米 → 脉冲，全在 calibration.js 里，
 * 这里只负责把点击位置喂进去、把脉冲发下去。
 *
 * **默认关着**（面板里那个勾）：这是会动真硬件的操作，误点一下机器就跑了，
 * 所以要有意勾上才生效。
 */
function onBoardClick(event) {
  if (!hostEl.calMove.checked) return
  const rect = lastResult?.board?.rect_size
  if (!rect) {
    hostLog('还没有识别结果，先点「识别一次」')
    return
  }
  const box = el.board.getBoundingClientRect()
  if (!box.width || !box.height) return
  const bx = ((event.clientX - box.left) / box.width) * rect[0]
  const by = ((event.clientY - box.top) / box.height) * rect[1]
  if (bx < 0 || by < 0 || bx > rect[0] || by > rect[1]) return   // 点在板子外面

  const [xmm, ymm] = boardPxToMm(bx, by, rect[0], rect[1])
  const [px, py] = mmToPulses(xmm, ymm)
  const target = { x: Math.round(px), y: Math.round(py) }
  clickTarget = [bx, by]
  renderView()
  hostLog(`点击 → 板面(${xmm.toFixed(0)}, ${ymm.toFixed(0)}) mm → 脉冲(${target.x}, ${target.y})`)
  hostCommand({ cmd: 'set', x: target.x, y: target.y }).then(async () => {
    // 顺手补查一次：ACK 只代表命令**收到**了，位置得另外问。不补这一次的话，
    // 准星要等到下一次轮询才动，看着像没反应。想连续跟着走就开「自动轮询状态」。
    setTimeout(async () => {
      const reply = await hostCommand({ cmd: 'get_all' })
      if (reply?.status) {
        showStatus(reply.status)
        onMachinePose(reply.status)
      }
    }, 700)
  })
}

function wireHostConsole() {
  hostEl.toggle.addEventListener('click', async () => {
    const willShow = hostEl.panel.classList.contains('hidden')
    hostEl.panel.classList.toggle('hidden', !willShow)
    if (willShow && !hostStarted) {
      const ok = await window.camAPI.host.start()
      if (!ok) {
        hostLog('桥接起不来——多半是没找到 Python（要能导入 puzzle_host）')
        return
      }
      hostStarted = true
      await refreshPorts()
    }
  })
  hostEl.close.addEventListener('click', () => hostEl.panel.classList.add('hidden'))
  hostEl.refresh.addEventListener('click', refreshPorts)
  hostEl.connect.addEventListener('click', toggleConnection)
  hostEl.ping.addEventListener('click', () => hostCommand({ cmd: 'ping' }))
  hostEl.reinit.addEventListener('click', () => hostCommand({ cmd: 'reinit' }))
  hostEl.getAll.addEventListener('click', async () => {
    const reply = await hostCommand({ cmd: 'get_all' })
    if (reply?.status) {
      showStatus(reply.status)
      onMachinePose(reply.status)
    }
  })
  hostEl.echo.addEventListener('click', () => hostCommand({ cmd: 'echo', data: '11 22 33' }))
  hostEl.send.addEventListener('click', sendSettings)
  hostEl.poll.addEventListener('change', () => {
    hostCommand({ cmd: 'poll', interval_ms: hostEl.poll.checked ? POLL_INTERVAL_MS : 0 })
  })

  // 坐标映射那几个框：改完立刻存下来并重画，不用点「应用」
  for (const input of [hostEl.calXMin, hostEl.calXMax, hostEl.calYMin, hostEl.calYMax,
    hostEl.calBW, hostEl.calBH]) {
    input.addEventListener('change', applyCalibrationForm)
  }
  hostEl.calInvX.addEventListener('change', applyCalibrationForm)
  hostEl.calInvY.addEventListener('change', applyCalibrationForm)
  hostEl.calReset.addEventListener('click', () => {
    Object.assign(calibration, CAL_DEFAULTS)
    fillCalibrationForm()
    applyCalibrationForm()
  })
  hostEl.servoBtn.addEventListener('click', () => toggleActuator('servo'))
  hostEl.emBtn.addEventListener('click', () => toggleActuator('em'))

  el.board.addEventListener('click', onBoardClick)
  hostEl.calMove.addEventListener('change', () => {
    el.board.classList.toggle('movable', hostEl.calMove.checked)
    if (hostEl.calMove.checked) {
      hostLog('已开启「点击画面移动机器」——再点板面就会把机器开过去')
    } else {
      clickTarget = null
      renderView()
    }
  })

  window.camAPI.host.onLog(hostLog)
  window.camAPI.host.onEvent((message) => {
    if (message.event === 'status') {
      showStatus(message)
      onMachinePose(message)
    }
    else if (message.event === 'conn') {
      setConnected(message.state === 'up')
      hostLog(`连接：${message.state}${message.target ? ` (${message.target})` : ''}`)
    } else if (message.event === 'error') hostLog(`错误：${message.message}`)
    else if (message.event === 'log') hostLog(message.text)
    else if (message.event === 'echo') hostLog(`回环：${message.data}`)
  })

  // 把存下来的标定填进框里，并显示一次读数
  fillCalibrationForm()
  updateCalReadout()
  updateActuatorButtons()          // 初始态：没连接，两个开关是灰的
}

if (window.camAPI?.host) wireHostConsole()
