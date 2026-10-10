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

/**
 * 机器两个轴和板子两个方向是**交叉**的：机器的 X 电机走的是板子的**纵向**，
 * 机器的 Y 电机走**横向**（装的时候是这么接的，实测反馈：不交叉准星就横竖颠倒）。
 *
 * 所以下面归一化出来的两个比例，要分别当作「纵向比例」和「横向比例」来用。
 * 换上不交叉的机器就把这里改成 false。
 *
 * 注意：X/Y 两个「反向」勾选说的仍然是**机器的那两个轴**——交叉之后，
 * 「X 反向」翻的就是板子的纵向，这不是 bug，是同一个轴。
 */
const AXES_CROSSED = true

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
  // 一键校准的两个参数：Y 开到哪儿（挪出画面），以及等多久再抓帧
  calibYPulse: 32000,
  calibSettleMs: 4000,
  // 自动拼图里「吸起来之后、开始搬之前」停多久。
  //
  // 这个停留是必要的：吸的最后一步只等到命令被 ACK 就返回了，而 ACK 只代表命令**收到**，
  // 舵机这时候还没真正升到位。不等就转、就走，等于拖着碎片蹭 —— 实测反馈「吸起到放下
  // 之间时间太短」就是它。
  autoCarryDwellMs: 1000,
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
  // 先各自归一化到「机器 X 的比例」「机器 Y 的比例」
  let nx = _clamp01(_norm(px, calibration.xMin, calibration.xMax))
  let ny = _clamp01(_norm(py, calibration.yMin, calibration.yMax))
  if (calibration.invertX) nx = 1 - nx
  if (calibration.invertY) ny = 1 - ny
  // 再按「机器轴 ↔ 板子方向」的对应关系组装（见 AXES_CROSSED）
  const [hx, vy] = AXES_CROSSED ? [ny, nx] : [nx, ny]
  return [hx * calibration.boardW, vy * calibration.boardH]
}

/** 板面毫米 → 脉冲（反函数）。把碎片的目标点翻成机器要走的脉冲，用的就是它 */
function mmToPulses(xmm, ymm) {
  const hx = calibration.boardW ? xmm / calibration.boardW : 0
  const vy = calibration.boardH ? ymm / calibration.boardH : 0
  let [nx, ny] = AXES_CROSSED ? [vy, hx] : [hx, vy]   // 拆回机器那两个轴
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
