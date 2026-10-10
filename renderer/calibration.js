/**
 * 电机脉冲 ↔ 板面坐标的换算。
 *
 * 机器的 posX/posY 是脉冲，画面里的一切是板面像素，两者本来没关系。这里分两级接起来：
 *
 *   脉冲  ──(脉冲范围 → 板子的实际尺寸)──►  毫米  ──(毫米 → 板面像素)──►  画面
 *
 * **中间那层毫米是有意加的**。直接「脉冲 ↔ 板面像素」也能用，但像素这个量纲绑死在
 * 摄像头分辨率上：换个「分析分辨率」，同一个脉冲对应的像素值就变了，标定等于白做。
 * 毫米是物理量，只和板子、电机有关，跟摄像头一点关系没有——所以标定的落点定在毫米上，
 * 像素只是最后一步的显示换算。
 *
 * 单独一个文件是因为两边都要用：renderer.js 拿它画准星和网格，host-console.js 拿它做配置。
 */
const CALIBRATION_KEY = 'puzzle_cam.calibration'

const calibration = {
  // 脉冲范围：整块板子对应的电机行程
  xMin: 500,
  xMax: 2200,
  yMin: 2200,
  yMax: 25100,
  invertX: false,
  invertY: false,
  // 板子实际多大（毫米）。默认 A4 横向（297×210）；
  // 板子是竖着放的话改成 210 × 297 就行
  boardW: 297,
  boardH: 210,
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

const _norm = (value, lo, hi) => (hi - lo ? (value - lo) / (hi - lo) : 0)
const _clamp01 = (v) => Math.min(1, Math.max(0, v))

/** 脉冲 → 板面毫米。结果**夹在板内**：脉冲跑出标定范围时准星画到画布外面就看不见了 */
function pulsesToMm(px, py) {
  let nx = _clamp01(_norm(px, calibration.xMin, calibration.xMax))
  let ny = _clamp01(_norm(py, calibration.yMin, calibration.yMax))
  if (calibration.invertX) nx = 1 - nx
  if (calibration.invertY) ny = 1 - ny
  return [nx * calibration.boardW, ny * calibration.boardH]
}

/** 板面毫米 → 脉冲（反函数）。把碎片的目标点翻成机器要走的脉冲，用的就是它 */
function mmToPulses(xmm, ymm) {
  let nx = calibration.boardW ? xmm / calibration.boardW : 0
  let ny = calibration.boardH ? ymm / calibration.boardH : 0
  if (calibration.invertX) nx = 1 - nx
  if (calibration.invertY) ny = 1 - ny
  return [
    calibration.xMin + nx * (calibration.xMax - calibration.xMin),
    calibration.yMin + ny * (calibration.yMax - calibration.yMin),
  ]
}

/** 毫米 → 板面像素（只是显示用的缩放） */
function mmToBoardPx(xmm, ymm, rectW, rectH) {
  return [
    calibration.boardW ? (xmm / calibration.boardW) * rectW : 0,
    calibration.boardH ? (ymm / calibration.boardH) * rectH : 0,
  ]
}

/** 板面像素 → 毫米 */
function boardPxToMm(x, y, rectW, rectH) {
  return [
    rectW ? (x / rectW) * calibration.boardW : 0,
    rectH ? (y / rectH) * calibration.boardH : 0,
  ]
}

/** 脉冲 → 板面像素（画准星直接用这个） */
function pulsesToBoard(px, py, rectW, rectH) {
  const [xmm, ymm] = pulsesToMm(px, py)
  return mmToBoardPx(xmm, ymm, rectW, rectH)
}

/** 板面像素 → 脉冲 */
function boardToPulses(x, y, rectW, rectH) {
  const [xmm, ymm] = boardPxToMm(x, y, rectW, rectH)
  return mmToPulses(xmm, ymm)
}

/** 脉冲是否跑出了标定范围（映射本身已经夹住，这里只是告诉调用方「它不在范围内」） */
function pulseOutOfRange(px, py) {
  const out = (v, lo, hi) => v < Math.min(lo, hi) || v > Math.max(lo, hi)
  return out(px, calibration.xMin, calibration.xMax) ||
         out(py, calibration.yMin, calibration.yMax)
}

loadCalibration()
