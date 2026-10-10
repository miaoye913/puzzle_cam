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
  settingsToggle: document.getElementById('settings-toggle'),
  settings: document.getElementById('settings'),
  settingsClose: document.getElementById('settings-close'),
  connStatus: document.getElementById('host-status'),
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
  pickBtn: document.getElementById('act-pick'),
  placeBtn: document.getElementById('act-place'),
  calibBtn: document.getElementById('act-calib'),
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
  calCalibY: document.getElementById('cal-caliby'),
  calCalibWait: document.getElementById('cal-calibwait'),
  calCarryDwell: document.getElementById('cal-carrydwell'),
  calSettleDwell: document.getElementById('cal-settledwell'),
  calMoveWait: document.getElementById('cal-movewait'),
  calActionStep: document.getElementById('cal-actionstep'),
  calReadout: document.getElementById('cal-readout'),
  calMove: document.getElementById('cal-move'),
  rotMain: document.getElementById('rot-main'),
  rotApply: document.getElementById('rot-apply'),
  autoBtn: document.getElementById('act-auto'),
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
  if (performance.now() < actuatorGuardUntil) return   // 刚下发过：这条状态可能是过时的
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
// 操作过程中要随手按的，所以放在工具栏上，不在设置页里
const actuator = { servo: false, em: false }

// 刚下发过的执行器，这么长时间内不拿 status 事件去覆盖按钮。
//
// 为什么需要它：机器状态是「查回来的」，一次查询的响应事件可能比我们随后的点击**到得还晚**
// （实测踩到过：点两下关掉，机器确实关了，按钮却弹回「开」）。那条事件带的是点击**之前**
// 的旧状态，它没有错，只是过时了。隔一小段时间不看它，按钮就不会被翻回去。
// 开着自动轮询时，新状态一两秒内就会自己回来，正好接上。
let actuatorGuardUntil = 0
const ACTUATOR_GUARD_MS = 1200

// ── 吸 / 放：把「舵机放下 + 电磁铁 + 舵机升起」合成一个动作 ──────────
// 吸：舵机放下 → 电磁铁吸合 → 舵机升起
// 放：舵机放下 → 电磁铁释放 → 舵机升起
//
// 三步之间的间隔现在在设置页「吸/放步进」里可调（calibration.actionStepMs）。
// 说明见 README：舵机行程长短不一样，这个数得能当场调。
// 舵机哪个值是「放下」。接线不同可能正好相反——实测发现放不下/收不回就把这两个换过来
const SERVO_DOWN = 1
const SERVO_UP = 0

// 一键校准的两个参数放在设置页里可调（calibration.calibYPulse / calibSettleMs），
// 因为机器快慢不一样，得能当场调。
//
// 那个等待时间**宁可给长一点**：ACK 只代表命令收到、不代表走到了，
// 机器还在半路上就抓帧的话，识别到的是糊的，标定白做。实测反馈也是「经常没动到就开始校准了」。

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
let actionRunning = false        // 动作进行中：挡住重复触发

function updateActuatorButtons() {
  hostEl.servoBtn.classList.toggle('active', actuator.servo)
  hostEl.emBtn.classList.toggle('active', actuator.em)
  hostEl.servoBtn.textContent = actuator.servo ? '舵机 开' : '舵机 关'
  hostEl.emBtn.textContent = actuator.em ? '电磁铁 吸合' : '电磁铁 释放'
  hostEl.servoBtn.disabled = !hostConnected
  hostEl.emBtn.disabled = !hostConnected
  // 没连上就没得动，所以「点击移动」和「旋转」也一起灰掉
  hostEl.calMove.disabled = !hostConnected
  hostEl.rotApply.disabled = !hostConnected
  // 吸/放/一键校准：没连上、或者已经有动作在跑，就按不动
  for (const button of [hostEl.pickBtn, hostEl.placeBtn, hostEl.calibBtn]) {
    button.disabled = !hostConnected || actionRunning
  }
  // 自动拼图：跑起来之后**仍然可点**——那时候它是「停止」。
  // 平时显示「拼第几块」，一次只走一块，方便一步一步调。
  if (autoRunning) hostEl.autoBtn.textContent = '停止'
  else if (!autoTotal) hostEl.autoBtn.textContent = '自动拼图'
  else if (autoIndex >= autoTotal) hostEl.autoBtn.textContent = `已拼完 ${autoTotal} 块`
  else hostEl.autoBtn.textContent = `拼第 ${autoIndex + 1}/${autoTotal} 块`
  hostEl.autoBtn.classList.toggle('stop', autoRunning)
  hostEl.autoBtn.classList.toggle('primary', !autoRunning)
  hostEl.autoBtn.disabled = !hostConnected && !autoRunning
  // 连接状态放在主界面上：连接按钮搬到设置页了，不看一眼设置页也得知道连没连上
  hostEl.connStatus.textContent = hostConnected
    ? `已连接 · ${hostEl.port.value.trim() || '串口'}`
    : '未连接'
  hostEl.connStatus.classList.toggle('ok', hostConnected)
}

/** 吸或放：一整串动作当成一件事来做，中途失败就停在那儿并说明原因 */
async function runPickPlace(action) {
  if (actionRunning || !hostConnected) return
  const isPick = action === 'pick'
  const name = isPick ? '吸' : '放'
  actionRunning = true
  updateActuatorButtons()
  try {
    const steps = [
      { servo: SERVO_DOWN, text: '舵机放下' },
      { em: isPick ? 1 : 0, text: isPick ? '电磁铁吸合' : '电磁铁释放' },
      // 放的时候最后一步**顺便把旋转归零**：碎片已经放下、工具是空的，转起来不会蹭到东西。
      // 一条命令带两个字段，不额外占时间（整个动作仍然约 1.45 秒）。
      // 这一步也是自动拼图能成立的前提——每轮都从「旋转 0」开始，下一块拾取时的姿态才是确定的。
      isPick ? { servo: SERVO_UP, text: '舵机升起' }
             : { servo: SERVO_UP, rot: 0, text: '舵机升起，旋转归零' },
    ]
    for (let i = 0; i < steps.length; i++) {
      const step = steps[i]
      const payload = { cmd: 'set' }
      if (step.servo !== undefined) {
        payload.servo = step.servo
        actuator.servo = step.servo === 1
      }
      if (step.em !== undefined) {
        payload.em = step.em
        actuator.em = step.em === 1
      }
      if (step.rot !== undefined) {
        payload.rot = step.rot
        hostEl.rotMain.value = String(step.rot)
      }
      updateActuatorButtons()
      hostLog(`${name} ${i + 1}/${steps.length}：${step.text}`)
      const reply = await hostCommand(payload)
      if (!reply?.ok) {
        hostLog(`${name} 中断在第 ${i + 1} 步（${step.text}）`)
        return
      }
      actuatorGuardUntil = performance.now() + ACTUATOR_GUARD_MS
      if (i < steps.length - 1) await sleep(Math.max(0, Math.round(Number(calibration.actionStepMs) || 0)))
    }
    hostLog(`${name} 完成`)
  } finally {
    actionRunning = false
    updateActuatorButtons()
  }
}

// ── 自动拼图 ────────────────────────────────────────────────────────
// 对每一块碎片走一遍：旋转归零 → 去取 → 吸 → 转到目标姿态 → 去放 → 放（含旋转归零）

let autoRunning = false
let autoAbort = false
// 现在一次只拼一块，所以要有进度。换了一次识别结果就把进度清零——
// 碎片已经动过了，旧的路径作废。
let autoIndex = 0
let autoTotal = 0
let autoResult = null

/** 可被打断的等待：点了「停止」不该还傻等剩下的几秒 */
async function sleepAbortable(ms) {
  const deadline = performance.now() + ms
  let left = deadline - performance.now()
  while (!autoAbort && left > 0) {
    await sleep(Math.min(100, left))
    left = deadline - performance.now()
  }
}

/**
 * 发一条移动命令，等机器走完，再停一下。
 *
 * **只看时间，不问机器**。以前这里轮询 get_all 判「到位」，那是错的：协议里的
 * posX/posY 是**命令寄存器**，命令一发下去它就等于目标值了，跟机器实际走到哪没关系——
 * 所以第一次轮询就「判到位」，整个等待只剩一次轮询的时间，动作在机器还在路上时就下去了。
 * 实测反馈「一边停一边放」「后面的连吸都没停就吸」都是它。
 *
 * 现在是固定的两段，顺序执行，**每一块、每一步都一模一样**：
 *
 *   发命令 → 等「移动等待」估它走完 → 停「到位停留」等它停稳 → 交回调用方做动作
 *
 * 两个值都在设置页里可调。机器还没走到就把「移动等待」调大。
 */
async function moveAndWait(x, y, label) {
  const reply = await hostCommand({ cmd: 'set', x, y })
  if (!reply?.ok) return false

  const travel = Math.max(0, Math.round(Number(calibration.autoMoveMs) || 0))
  if (travel) {
    hostLog(`   ${label}：命令已发，等 ${travel}ms 走完`)
    await sleepAbortable(travel)
  }

  const settle = Math.max(0, Math.round(Number(calibration.autoSettleMs) || 0))
  if (settle && !autoAbort) {
    hostLog(`   ${label}到位，停 ${settle}ms 等停稳`)
    await sleepAbortable(settle)
  }
  return true
}

async function autoAssemble() {
  if (autoRunning) {          // 再点一下 = 停
    autoAbort = true
    hostLog('自动拼图：收到停止，走完当前这步就停')
    return
  }
  if (!hostConnected) { hostLog('自动拼图：还没连上机器（去设置页连一下）'); return }
  if (!lastResult?.board?.found) { hostLog('自动拼图：还没有识别结果，先点「识别一次」'); return }
  if (!lastResult.assembly) { hostLog('自动拼图：这几块没拼出矩形，没得拼'); return }

  const ctx = currentAssemblyMap(lastResult)
  if (!ctx) { hostLog('自动拼图：拿不到板面尺寸，先识别一次'); return }
  const rect = lastResult.board.rect_size
  const paths = planPaths(lastResult, ctx.map)
  if (!paths.length) { hostLog('自动拼图：没有可搬运的路径'); return }

  if (autoIndex >= paths.length) {
    hostLog(`自动拼图：${paths.length} 块都拼过了。要重来先重新识别一次（进度会跟着重置）`)
    return
  }

  // **一次只拼一块**：点一下走一块，方便对着日志和画面一步一步调。点完进度自己往前走。
  const path = paths[autoIndex]
  const step = autoIndex + 1
  autoRunning = true
  autoAbort = false
  updateActuatorButtons()
  hostLog(`—— 第 ${step}/${paths.length} 块（#${path.id}）——`)
  try {
    // 1. 旋转归零：每轮都从同一个姿态开始，转角才有确定含义
    await hostCommand({ cmd: 'set', rot: 0 })
    hostEl.rotMain.value = '0'

    // 2. 去碎片现在的位置，3. 吸起来
    const [fx, fy] = boardToPulses(path.from[0], path.from[1], rect[0], rect[1])
    const from = [Math.round(fx), Math.round(fy)]
    hostLog(`   去取 #${path.id}：脉冲(${from[0]}, ${from[1]})`)
    if (await moveAndWait(from[0], from[1], '去取') && !autoAbort) {
      await runPickPlace('pick')
      // 吸起来之后停一下再走：`吸` 的最后一步只等到命令被 ACK，舵机这时还没真正升到位，
      // 不等就转、就走等于拖着碎片蹭。时长在设置页可调。
      if (!autoAbort) {
        const dwell = Math.max(0, Math.round(Number(calibration.autoCarryDwellMs) || 0))
        if (dwell) {
          hostLog(`   吸住了，停 ${dwell}ms 等舵机升到位`)
          await sleepAbortable(dwell)
        }
      }
    }

    // 4. 转到目标姿态（拾取时工具在 0°，所以直接转到 dRot 就是目标姿态）
    if (!autoAbort) {
      const rot = Math.round(path.dRot)
      hostLog(`   旋转 → ${rot}°`)
      await hostCommand({ cmd: 'set', rot })
      hostEl.rotMain.value = String(rot)
    }

    // 5. 去目标位置，6. 放下（放的过程里会把旋转归零）
    if (!autoAbort) {
      const [tx, ty] = boardToPulses(path.to[0], path.to[1], rect[0], rect[1])
      const to = [Math.round(tx), Math.round(ty)]
      hostLog(`   去放 #${path.id}：脉冲(${to[0]}, ${to[1]})`)
      if (await moveAndWait(to[0], to[1], '去放') && !autoAbort) {
        await runPickPlace('place')
      }
    }

    if (autoAbort) {
      hostLog(`自动拼图：已停止（第 ${step} 块没走完，下次还是从这一块开始）`)
    } else {
      autoIndex = step          // 只有整块走完才往前走
      hostLog(`自动拼图：第 ${step}/${paths.length} 块完成` +
              (autoIndex < paths.length ? `，再点一下拼第 ${autoIndex + 1} 块` : '，全部拼完'))
    }
  } finally {
    autoRunning = false
    autoAbort = false
    updateActuatorButtons()
  }
}

/**
 * 把每块碎片的「未拼」（现在在哪）和「已拼」（拼好该在哪）填进设置页那张表。
 *
 * 毫米和脉冲都给：毫米是画面上的位置，脉冲是发给机器的值。
 * 自动拼图出问题时，先看这两列——**目标脉冲对不对**一眼就能判断是换算错了还是机器没走到。
 */
function updatePathTable(result) {
  const body = document.getElementById('path-table')
  if (!body) return
  const reset = (text) => {
    body.replaceChildren()
    const tr = document.createElement('tr')
    const td = document.createElement('td')
    td.colSpan = 4
    td.className = 'muted'
    td.textContent = text
    tr.append(td)
    body.append(tr)
  }
  const ctx = result?.board?.found ? currentAssemblyMap(result) : null
  if (!ctx) { autoTotal = 0; reset('还没识别'); return }
  const rect = result.board.rect_size
  const paths = planPaths(result, ctx.map)
  if (!paths.length) { autoTotal = 0; reset('没拼出矩形'); return }

  // 换了一份识别结果就把自动拼图的进度清零：碎片已经被搬过，旧路径作废了
  if (result !== autoResult) {
    autoResult = result
    autoIndex = 0
  }
  autoTotal = paths.length

  body.replaceChildren()
  for (const path of paths) {
    const tr = document.createElement('tr')
    const cell = (content) => {
      const td = document.createElement('td')
      td.textContent = content
      tr.append(td)
    }
    const coords = (bx, by) => {
      const [mx, my] = boardPxToMm(bx, by, rect[0], rect[1])
      const [px, py] = boardToPulses(bx, by, rect[0], rect[1])
      return `${mx.toFixed(0)}, ${my.toFixed(0)} / ${Math.round(px)}, ${Math.round(py)}`
    }
    cell(`#${path.id}`)
    cell(coords(path.from[0], path.from[1]))
    cell(coords(path.to[0], path.to[1]))
    cell(`${path.dRot >= 0 ? '+' : ''}${path.dRot.toFixed(0)}°`)
    body.append(tr)
  }
}

/**
 * 一键校准：Y 开到标定位置（挪出画面）→ 等它走完 → 识别一次 → 再移到板子中央。
 *
 * 最后那步移到中央是为了方便接着动手——校准完工具停在画面外，什么也够不着。
 */
async function oneClickCalibrate() {
  if (actionRunning || !hostConnected) return
  actionRunning = true
  updateActuatorButtons()
  const yTarget = Math.round(Number(calibration.calibYPulse) || 0)
  const waitMs = Math.max(0, Math.round(Number(calibration.calibSettleMs) || 0))
  try {
    hostLog(`一键校准：Y → 脉冲 ${yTarget}，等 ${waitMs}ms`)
    const reply = await hostCommand({ cmd: 'set', y: yTarget })
    if (!reply?.ok) {
      hostLog('一键校准中断：Y 没发出去')
      return
    }
    // 等机器真的走过去。宁可多等——半路上抓帧的话识别到的是糊的，白做
    await sleep(waitMs)
    hostLog('一键校准：开始识别')
    await recognizeOnce()

    // 停到板子中央，方便下一步动手
    const rect = lastResult?.board?.rect_size
    if (rect) {
      const [cx, cy] = boardToPulses(rect[0] / 2, rect[1] / 2, rect[0], rect[1])
      const centre = [Math.round(cx), Math.round(cy)]
      hostLog(`一键校准：移到中央 脉冲(${centre[0]}, ${centre[1]})`)
      await hostCommand({ cmd: 'set', x: centre[0], y: centre[1] })
    } else {
      hostLog('一键校准：没识别出板面，跳过「移到中央」')
    }
  } finally {
    actionRunning = false
    updateActuatorButtons()
  }
}

async function toggleActuator(which) {
  if (actionRunning) return          // 动作进行中别插手，免得把时序打乱
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
  actuatorGuardUntil = performance.now() + ACTUATOR_GUARD_MS
  hostLog(`${which === 'servo' ? '舵机' : '电磁铁'} → ${next ? '开' : '关'}`)
}

// ── 坐标映射（脉冲 ↔ 板面）────────────────────────────────────────
const CAL_DEFAULTS = {
  xMin: 500, xMax: 2200, yMin: 2200, yMax: 25100,
  invertX: false, invertY: false, boardW: 297, boardH: 210,
  calibYPulse: 32000, calibSettleMs: 4000,
  autoMoveMs: 4000, autoCarryDwellMs: 1000, autoSettleMs: 800, actionStepMs: 700,
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
  hostEl.calCalibY.value = calibration.calibYPulse
  hostEl.calCalibWait.value = calibration.calibSettleMs
  hostEl.calCarryDwell.value = calibration.autoCarryDwellMs
  hostEl.calSettleDwell.value = calibration.autoSettleMs
  hostEl.calMoveWait.value = calibration.autoMoveMs
  hostEl.calActionStep.value = calibration.actionStepMs
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
  calibration.calibYPulse = num(hostEl.calCalibY, calibration.calibYPulse)
  calibration.calibSettleMs = num(hostEl.calCalibWait, calibration.calibSettleMs)
  calibration.autoCarryDwellMs = num(hostEl.calCarryDwell, calibration.autoCarryDwellMs)
  calibration.autoSettleMs = num(hostEl.calSettleDwell, calibration.autoSettleMs)
  calibration.autoMoveMs = num(hostEl.calMoveWait, calibration.autoMoveMs)
  calibration.actionStepMs = num(hostEl.calActionStep, calibration.actionStepMs)
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
/** 「点击移动」是个开关按钮，开着的标志是 .active（原来是复选框的 .checked） */
const moveArmed = () => hostEl.calMove.classList.contains('active')

function onBoardClick(event) {
  if (!moveArmed()) return
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
  hostEl.settingsToggle.addEventListener('click', async () => {
    const willShow = hostEl.settings.classList.contains('hidden')
    hostEl.settings.classList.toggle('hidden', !willShow)
    hostEl.settingsToggle.classList.toggle('active', willShow)
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
  hostEl.settingsClose.addEventListener('click', () => {
    hostEl.settings.classList.add('hidden')
    hostEl.settingsToggle.classList.remove('active')
  })
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
    hostEl.calBW, hostEl.calBH, hostEl.calCalibY, hostEl.calCalibWait,
    hostEl.calCarryDwell, hostEl.calSettleDwell, hostEl.calMoveWait, hostEl.calActionStep]) {
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
  // 主界面上填个角度直接转，不用开设置页
  hostEl.rotApply.addEventListener('click', async () => {
    const deg = Math.round(Number(hostEl.rotMain.value) || 0)
    const reply = await hostCommand({ cmd: 'set', rot: deg })
    if (reply?.ok) {
      hostEl.rotMain.value = deg
      hostEl.rot.value = deg        // 和设置页那个框同步，免得两处显示不一样
      hostLog(`旋转 → ${deg}°`)
    }
  })

  hostEl.pickBtn.addEventListener('click', () => runPickPlace('pick'))
  hostEl.placeBtn.addEventListener('click', () => runPickPlace('place'))
  hostEl.calibBtn.addEventListener('click', oneClickCalibrate)
  hostEl.autoBtn.addEventListener('click', autoAssemble)

  el.board.addEventListener('click', onBoardClick)
  // 「点击移动」现在是开关按钮（原来是设置页里的复选框）——操作时随时要切，
  // 不该埋在设置页里
  hostEl.calMove.addEventListener('click', () => {
    hostEl.calMove.classList.toggle('active')
    const armed = moveArmed()
    el.board.classList.toggle('movable', armed)
    if (armed) {
      hostLog('已开启「点击移动」——再点板面就会把机器开过去')
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
