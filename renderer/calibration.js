/**
 * 电机脉冲 ↔ 板面坐标的换算。
 *
 * 机器的 posX/posY 是脉冲，画面里的一切是板面像素，两者本来没关系。这里用「两段标定」
 * 把它们接起来：告诉程序 X 脉冲的起止范围对应板子的横向、Y 的起止范围对应纵向，
 * 中间按线性插值。
 *
 * 单独一个文件是因为两边都要用：renderer.js 拿它画准星，host-console.js 拿它做配置界面。
 *
 * 反向换算 boardToPulses 现在还没人用，但它是同一套映射的反函数，三行就写完——
 * 「把碎片的目标点翻译成机器要走的脉冲」正是下一步驱动机器时缺的东西，先摆在这儿。
 */
const CALIBRATION_KEY = 'puzzle_cam.calibration'

const calibration = {
  xMin: 500,
  xMax: 2200,
  yMin: 2200,
  yMax: 25100,
  invertX: false,
  invertY: false,
}

/** 机器此刻在哪（脉冲）。没连机器就是 null */
let machinePose = null

function loadCalibration() {
  try {
    const raw = localStorage.getItem(CALIBRATION_KEY)
    if (raw) {
      const saved = JSON.parse(raw)
      for (const key of Object.keys(calibration)) {
        if (saved[key] !== undefined && saved[key] !== null) calibration[key] = saved[key]
      }
    }
  } catch {
    // 存坏了就用默认值，不该因为一条坏记录起不来
  }
  return calibration
}

function saveCalibration() {
  try {
    localStorage.setItem(CALIBRATION_KEY, JSON.stringify(calibration))
  } catch {
    // 隐私模式之类写不进去，忽略即可
  }
}

function _norm(value, lo, hi) {
  const span = hi - lo
  return span ? (value - lo) / span : 0
}

const _clamp01 = (v) => Math.min(1, Math.max(0, v))

/**
 * 脉冲 → 板面像素。结果**夹在板内**：脉冲跑出标定范围时准星画到画布外面就看不见了，
 * 夹住之后至少还看得见它贴在边上，配合 pulseOutOfRange() 提示「已经超出范围」。
 */
function pulsesToBoard(px, py, rectW, rectH) {
  let nx = _clamp01(_norm(px, calibration.xMin, calibration.xMax))
  let ny = _clamp01(_norm(py, calibration.yMin, calibration.yMax))
  if (calibration.invertX) nx = 1 - nx
  if (calibration.invertY) ny = 1 - ny
  return [nx * rectW, ny * rectH]
}

/** 板面像素 → 脉冲（上面那个的反函数） */
function boardToPulses(x, y, rectW, rectH) {
  let nx = rectW > 0 ? x / rectW : 0
  let ny = rectH > 0 ? y / rectH : 0
  if (calibration.invertX) nx = 1 - nx
  if (calibration.invertY) ny = 1 - ny
  return [
    calibration.xMin + nx * (calibration.xMax - calibration.xMin),
    calibration.yMin + ny * (calibration.yMax - calibration.yMin),
  ]
}

/** 脉冲是否跑出了标定范围（映射本身已经夹住，这里只是告诉调用方「它不在范围内」） */
function pulseOutOfRange(px, py) {
  const out = (v, lo, hi) => v < Math.min(lo, hi) || v > Math.max(lo, hi)
  return out(px, calibration.xMin, calibration.xMax) ||
         out(py, calibration.yMin, calibration.yMax)
}

loadCalibration()
