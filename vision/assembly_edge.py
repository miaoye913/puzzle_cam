"""
碎片拼接（第二种做法）：**边长 + 角度匹配 + 回溯**，凑不成矩形就明确失败。

移植自公开的 2026 电赛 E 题方案（26_ElectronicDesign_Competition_E）的**思路**：
那边不是「随便摆一个再看它像不像矩形」，而是

  1. 先放一块当底（面积最大的那块），它的整圈轮廓就是第一版「前沿」；
  2. 只允许把剩下的块贴到**当前外轮廓上露出来的边**上——内部的接缝不算候选；
  3. 边长差在容差内才有戏；再看这条边**两端的两个内角**，和前沿那条边两端的
     内角相加，能不能凑出 90° 或 180°。两端都对上（两端角）优先于只对上一端；
  4. 按这个配对解出唯一的刚体变换，检查有没有压到已经摆好的块、接触面够不够长；
  5. 递归摆下一块；全摆完之后**验一遍是不是矩形**，不是就退回来换下一个候选；
  6. 所有候选都试完还是没有矩形，就换一块当底，全试完还不行就返回 None。

代码是自己写的，没有拷贝对方的实现。和 `assembly.py` 里那套「束搜索 + 打分」的
根本区别在最后一步：打分那套**总能挑出一个「最不难看」的摆法**，所以一堆错误摆法里
它照样会交出一个来；这一套**要求真的凑成矩形**，凑不出来就老老实实说拼不出来。
实测反馈的「拼出来的矩形不对」正是前者的固有毛病。

两者是互补的：这个模块找不到矩形时，`assembly.solve()` 会退回打分搜索兜底，
并在结果里标 `strict = False`，让上层知道这个矩形是「凑合」的。
"""

from __future__ import annotations

import time

import numpy as np
import cv2

# 板子是 A4 横向 297×210mm —— 整个系统都按这个换算（calibration.boardW 默认 297，
# 前端的「板面像素 ↔ 毫米」用的就是它）。这里只用来把毫米级的容差换算成像素。
BOARD_W_MM = 297.0

# 默认参数，都是从对方那套方案搬过来的**物理量**（毫米 / 度），用的时候乘 px_per_mm。
# 它们是「这块拼图有多大」的量级，换一套大小差很多的拼图要重新量。
DEFAULTS = {
    'length_tol_mm': 4.0,          # 两条边长度差在这个以内才算配得上
    'angle_tol_deg': 4.0,          # 两端内角之和离 90/180 差多少度还算「凑上了」
    'min_shared_mm': 10.0,         # 贴合处的接触长度下限：只蹭到一个角不算贴上
    'max_overlap_ratio': 0.10,     # 新块和已摆好的重叠超过已摆部分面积的这个比例就否掉
    'collinear_angle_deg': 10.0,   # 外轮廓上「几乎是直线」的顶点：离 180° 多近就并掉
    'collinear_dist_mm': 4.0,      # 并顶点时，顶点离弦的距离上限
    'endpoint_join_mm': 5.0,       # 外轮廓上这种长度的短边，允许当成「接缝的残渣」抹掉
    'frontier_eps_mm': 1.0,        # 外轮廓多边形拟合的精度
    'close_radius_mm': 1.0,        # 验矩形前先做一次闭运算，把接缝的小豁口补上
    'fill_ratio': 0.90,            # 并集面积 / 外接矩形面积 的下限
    'mask_margin_mm': 12.0,        # 栅格掩膜四周留多少余量
}
# 搜索上限：这是纯回溯，最坏是指数级。给节点数和时间双重上限，到点了就当「没找到」，
# 交给兜底那套——宁可用老办法给个结果，也不能让一次识别卡在那儿
MAX_NODES = 20000
TIME_BUDGET_S = 6.0

# 进来之前先把碎片多边形「洗干净」用的阈值，和 pipeline.simplify_polygon 那套保持一致。
#
# 为什么非洗不可：这套算法是拿**整条边**去配的。一条直边中间只要多出一个顶点（内角
# 180°），这条边就被切成两半，两边都对不上任何一条完整的边了——**几何形状其实没变**，
# 但搜索会直接判死。实测：给这四块每块插进去几个直边中点，求解器 7ms 就返回 None；
# 洗一遍回到 4/3/4/4 个顶点，立刻又能拼出来。
COLLINEAR_DEG = 160.0        # 内角大于它 = 其实是直边上的抖动
SHORT_EDGE_RATIO = 0.25      # 边短到相邻边的这个比例以下，才**考虑**合并
TINY_AREA_RATIO = 0.015      # 并掉这条边造成的面积变化低于整块的这个比例，才算「不承载形状」


# ── 小工具 ──────────────────────────────────────────────────────────

def _unit(v):
    n = float(np.linalg.norm(v))
    return None if n <= 1e-9 else np.asarray(v, np.float64) / n


def _rot(theta_deg):
    t = np.radians(theta_deg)
    c, s = np.cos(t), np.sin(t)
    return np.array([[c, -s], [s, c]], np.float64)


def _bearing(v):
    """向量指向哪个方向（度）"""
    return float(np.degrees(np.arctan2(float(v[1]), float(v[0]))))


def _interior_angles(verts):
    """每个顶点一个内角。多边形绕向反了也不影响——算的是夹角的绝对值"""
    n = len(verts)
    out = np.zeros(n, np.float64)
    for i in range(n):
        a = _unit(verts[(i - 1) % n] - verts[i])
        b = _unit(verts[(i + 1) % n] - verts[i])
        if a is None or b is None:
            continue
        out[i] = np.degrees(np.arccos(np.clip(float(np.dot(a, b)), -1.0, 1.0)))
    return out


def _edge_lengths(verts):
    return np.linalg.norm(np.roll(verts, -1, axis=0) - verts, axis=1)


def _signed_area(verts):
    x, y = verts[:, 0], verts[:, 1]
    return float((np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))) * 0.5)


def _line_cross(p1, u, p2, v):
    """两条直线（点 + 方向）的交点；平行就返回 None"""
    den = u[0] * v[1] - u[1] * v[0]
    if abs(den) < 1e-9:
        return None
    t = ((p2[0] - p1[0]) * v[1] - (p2[1] - p1[1]) * v[0]) / den
    return p1 + t * u


def _clean_polygon(poly):
    """
    把碎片多边形洗成「只剩真正的角点」。两步，反复做：

      ① 内角大过 `COLLINEAR_DEG` 的顶点——那其实落在直边上，是采样抖动。
         不删掉的话一条边会被切成两段，边长匹配整条全废。
      ② 不承载形状的超短边——真角点被噪声拆成两个时，两个内角都没到共线的程度，
         ① 删不掉它们。这时把这条短边两端的顶点换成两条邻边延长线的交点。

    ② 的判据不能只看「这条边短不短」：**形状上真的有一条很短的边**（实测那块细长条的
    一条边只有 23px，而邻边 104px）也会满足长度比例，一刀切下去就把真角点吃掉了——
    踩过这个坑，代价是原本能拼出来的那组直接变成拼不出来。所以照 pipeline 的判据来：
    把这条短边换成一个点，**看面积变了多少**。真边一去掉面积立刻变化一大截；
    噪声造出来的假边去掉后几乎没变化。

    和 pipeline.simplify_polygon 是同一件事，这里再来一遍是**兜底**：识别那边已经洗过了，
    但求解器不该依赖「上游一定洗干净了」——真漏一个顶点进来，代价是整个拼不出来。
    洗干净的输入过一遍完全不变（幂等），所以不会把好数据改坏。
    """
    pts = [np.asarray(p, np.float64) for p in poly]
    # 每删掉/并掉一个顶点就重头扫一遍（下标会挪），所以圈数要够：一圈最少去掉一个
    for _ in range(2 * len(pts) + 8):
        if len(pts) <= 3:
            break
        # ① 近共线顶点
        dropped = False
        for i in range(len(pts)):
            prev, cur, nxt = pts[(i - 1) % len(pts)], pts[i], pts[(i + 1) % len(pts)]
            back, forth = _unit(prev - cur), _unit(nxt - cur)
            if back is None or forth is None:
                del pts[i]
                dropped = True
                break
            angle = np.degrees(np.arccos(np.clip(float(np.dot(back, forth)), -1.0, 1.0)))
            if angle > COLLINEAR_DEG:
                del pts[i]
                dropped = True
                break
        if dropped:
            continue

        # ② 超短边：先按长度比例粗筛，再**看面积**——真边一去掉面积立刻变化一大截
        n = len(pts)
        area = abs(_signed_area(np.asarray(pts, np.float64)))
        if n <= 3 or area <= 0:
            break
        lens = [float(np.linalg.norm(pts[(i + 1) % n] - pts[i])) for i in range(n)]
        merged = False
        for i in range(n):
            j = (i + 1) % n
            if lens[i] > SHORT_EDGE_RATIO * min(lens[i - 1], lens[j]):
                continue
            a, b = pts[(i - 1) % n], pts[i]
            c, d = pts[j], pts[(j + 1) % n]
            u, v = _unit(b - a), _unit(d - c)
            if u is None or v is None:
                continue
            hit = _line_cross(a, u, c, v)
            # 交点飞得太远说明这两条邻边快平行了，那不是「短边残渣」，别动它
            if hit is None or max(float(np.linalg.norm(hit - b)),
                                  float(np.linalg.norm(hit - c))) > 4.0 * lens[i]:
                continue
            # 这块被「抹平」的三角形占整块多大：够小才说明这条边不承载形状
            if abs(cv2.contourArea(np.asarray([b, hit, c], np.float32).reshape(-1, 1, 2))) \
                    >= TINY_AREA_RATIO * area:
                continue
            keep = [k for k in range(n) if k not in (i, j)]
            pts = [pts[k] for k in keep]
            pts.insert(i if i < j else 0, hit)
            merged = True
            break
        if not merged:
            break
    return np.asarray(pts, np.float64)


def _make_opts(px_per_mm, params=None, length_tol_mm=None):
    """把毫米/度的物理量换成这套坐标下的像素/度"""
    d = DEFAULTS
    opts = {
        'px_per_mm': float(px_per_mm),
        'length_tol': (length_tol_mm if length_tol_mm else d['length_tol_mm']) * px_per_mm,
        'angle_tol': d['angle_tol_deg'],
        'min_shared': d['min_shared_mm'] * px_per_mm,
        'max_overlap_ratio': d['max_overlap_ratio'],
        'collinear_angle': d['collinear_angle_deg'],
        'collinear_dist': d['collinear_dist_mm'] * px_per_mm,
        'endpoint_join': d['endpoint_join_mm'] * px_per_mm,
        'frontier_eps': max(1.0, d['frontier_eps_mm'] * px_per_mm),
        'close_radius': max(1, int(round(d['close_radius_mm'] * px_per_mm))),
        'fill_ratio': d['fill_ratio'],
        'mask_margin': d['mask_margin_mm'] * px_per_mm,
    }
    if params:
        opts.update(params)
    return opts


# ── 数据结构 ────────────────────────────────────────────────────────

class Piece:
    """一块碎片：顶点 + 每个顶点的内角 + 每条边的长度（第 i 条边 = 顶点 i → i+1）"""

    __slots__ = ('id', 'verts', 'angles', 'lengths', 'area')

    def __init__(self, pid, verts):
        self.id = pid
        self.verts = np.asarray(verts, np.float64).reshape(-1, 2)
        self.angles = _interior_angles(self.verts)
        self.lengths = _edge_lengths(self.verts)
        self.area = abs(_signed_area(self.verts))
        if self.area <= 0:
            raise ValueError('退化多边形')


class Placed:
    """已经摆好的一块：相对它自己原始形状转了多少度、平移了多少，以及落在哪儿"""

    __slots__ = ('src', 'theta', 'translation', 'verts')

    def __init__(self, src, theta, translation):
        self.src = src
        self.theta = float(theta)
        self.translation = np.asarray(translation, np.float64)
        self.verts = src.verts @ _rot(theta).T + self.translation


class FrontierEdge:
    """当前外轮廓上一条露出来的边。`src_piece` 标出它来自哪块的哪条边（合并出来的记 -1）"""

    __slots__ = ('a', 'b', 'length', 'a_angle', 'b_angle', 'src_piece', 'src_edge')

    def __init__(self, a, b, a_angle, b_angle, src_piece=-1, src_edge=-1):
        self.a = np.asarray(a, np.float64)
        self.b = np.asarray(b, np.float64)
        self.length = float(np.linalg.norm(self.b - self.a))
        self.a_angle = float(a_angle)
        self.b_angle = float(b_angle)
        self.src_piece = int(src_piece)
        self.src_edge = int(src_edge)


class Candidate:
    """一次「把这块的这条边贴到前沿那条边上」的尝试"""

    __slots__ = ('placed', 'frontier', 'src_edge', 'pairing', 'two_corner',
                 'length_err', 'other_err', 'angle_score')

    def __init__(self, placed, frontier, src_edge, pairing, two_corner,
                 length_err, other_err, angle_score):
        self.placed = placed
        self.frontier = frontier
        self.src_edge = src_edge
        self.pairing = pairing
        self.two_corner = two_corner
        self.length_err = float(length_err)
        self.other_err = float(other_err)
        self.angle_score = float(angle_score)

    def pose_key(self, eps=0.5):
        """同一种摆法可能被「原子边」和「合并边」各生成一次，靠它去重"""
        return tuple(np.rint(self.placed.verts.reshape(-1) / eps).astype(np.int64))


class State:
    """当前已经拼好的一坨：掩膜 + 露出来的边 + 还没用的块"""

    __slots__ = ('placed', 'remaining', 'mask', 'origin', 'atomic', 'merged',
                 'band', 'area_px')

    def __init__(self, placed, remaining, opts):
        self.placed = tuple(placed)
        self.remaining = tuple(remaining)
        self.mask, self.origin = _rasterize([p.verts for p in self.placed], opts)
        # 「已摆部分的轮廓」和「它的接触带」只和已摆的部分有关，和候选无关：
        # 在这里算一次，别放到逐个候选的循环里重算（那是热路径）
        self.band = cv2.dilate(self.mask, np.ones((3, 3), np.uint8), iterations=1)
        self.area_px = int(cv2.countNonZero(self.mask))
        self.atomic = _atomic_exposed_edges(self.placed, self.mask, self.origin, opts)
        self.merged = _merged_frontier(self.mask, self.origin, opts)

    @property
    def ids(self):
        return tuple(p.src.id for p in self.placed)

    def key(self):
        """与「哪块先放」无关的状态指纹：两块独立的块换个顺序放，还是同一个状态"""
        items = sorted(self.placed, key=lambda p: p.src.id)
        return tuple((p.src.id,
                      tuple(np.rint(p.verts.reshape(-1) / 0.5).astype(np.int64)))
                     for p in items)


# ── 栅格与轮廓 ──────────────────────────────────────────────────────

def _rasterize(polys, opts):
    """
    把几块多边形画到一张掩膜上。1:1 板面像素就够了（板面本身约 2.5 px/mm）。

    四周留的余量 = 12mm + **最大那块的对角线**：这样新块贴到外轮廓上之后也一定还在
    掩膜范围里，逐个候选做重叠检查时就不用重新栅格化了。
    """
    pts = np.vstack(polys)
    span = float(np.linalg.norm(pts.max(axis=0) - pts.min(axis=0)))
    margin = opts['mask_margin'] + span + opts['length_tol']
    lo = pts.min(axis=0) - margin
    hi = pts.max(axis=0) + margin
    size = np.maximum(np.ceil(hi - lo).astype(np.int32) + 2, 4)
    mask = np.zeros((int(size[1]), int(size[0])), np.uint8)
    for poly in polys:
        cv2.fillPoly(mask, [np.rint(poly - lo).astype(np.int32).reshape(-1, 1, 2)], 255)
    return mask, lo


def _contains(mask, origin, point):
    px = np.rint(point - origin).astype(np.int32)
    x, y = int(px[0]), int(px[1])
    if x < 0 or y < 0 or x >= mask.shape[1] or y >= mask.shape[0]:
        return False
    return bool(mask[y, x])


def _atomic_exposed_edges(placed, mask, origin, opts):
    """
    每块**自己的**每一条边，判断它是不是还露在外面。

    判法：沿边中点往两侧的法向各走一点，两边都有料 = 内部接缝（不算候选），
    只有一边有料 = 露在外面（可以贴）。

    这一步保留的是「原始边」，所以外轮廓上两块拼成一条长边之后，其中每一段短边仍然
    是独立的候选——对方方案里管这叫原子边。
    """
    probe = max(1.5, 0.6 * opts['px_per_mm'])
    edges = []
    for item in placed:
        verts = item.verts
        n = len(verts)
        for i in range(n):
            a, b = verts[i], verts[(i + 1) % n]
            d = _unit(b - a)
            if d is None:
                continue
            mid = (a + b) * 0.5
            normal = np.array([-d[1], d[0]])
            left = _contains(mask, origin, mid + normal * probe)
            right = _contains(mask, origin, mid - normal * probe)
            if left == right:      # 两边一样：要么是接缝（两边都有料），要么根本不在轮廓上
                continue
            edges.append(FrontierEdge(a, b,
                                      item.src.angles[i],
                                      item.src.angles[(i + 1) % n],
                                      item.src.id, i))
    return tuple(edges)


def _merged_frontier(mask, origin, opts):
    """
    外轮廓**合并**之后的那几条边。

    两块并排贴上之后，从外面看是一条长边，可原子边只有两段短的。第三块要是带着一条
    正好那么长的边，只有靠这里的合并边才贴得上去。
    """
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return ()
    contour = max(contours, key=cv2.contourArea)
    poly = cv2.approxPolyDP(contour, opts['frontier_eps'], True).reshape(-1, 2)
    if len(poly) < 3:
        return ()
    verts = _merge_straight(poly.astype(np.float64) + origin, opts)
    if len(verts) < 3:
        return ()
    angles = _interior_angles(verts)
    out = []
    for i in range(len(verts)):
        a, b = verts[i], verts[(i + 1) % len(verts)]
        if np.linalg.norm(b - a) <= 1e-6:
            continue
        out.append(FrontierEdge(a, b, angles[i], angles[(i + 1) % len(verts)]))
    return tuple(out)


def _merge_straight(verts, opts):
    """
    把外轮廓上「其实是一条直线」的顶点并掉。

    为什么要并：两块拼在一起之后，外轮廓在接缝那儿常常留下一个几乎 180° 的浅角，
    或者几毫米长的一小段台阶。不并掉的话，一条完整的直边会被切成好几个小段，
    后面按边长匹配就全对不上了。

    两个方向都并：① 顶点本身接近 180°、且离两端连线很近；② 夹在两条共线长边之间的
    一小段（接缝没对齐留下的残渣）。
    """
    points = [np.asarray(p, np.float64) for p in verts]
    changed = True
    while changed and len(points) > 3:
        changed = False
        for i in range(len(points)):                      # ①
            prev = points[(i - 1) % len(points)]
            cur = points[i]
            nxt = points[(i + 1) % len(points)]
            back = _unit(prev - cur)
            forth = _unit(nxt - cur)
            chord = _unit(nxt - prev)
            if back is None or forth is None or chord is None:
                continue
            angle = np.degrees(np.arccos(np.clip(float(np.dot(back, forth)), -1.0, 1.0)))
            rel = cur - prev
            dist = abs(rel[0] * chord[1] - rel[1] * chord[0])
            between = float(np.dot(cur - prev, nxt - cur)) >= -1e-6
            if (abs(angle - 180.0) <= opts['collinear_angle']
                    and dist <= opts['collinear_dist'] and between):
                del points[i]
                changed = True
                break
        if changed:
            continue
        for i in range(len(points)):                      # ②
            prev = points[(i - 1) % len(points)]
            j1 = points[i]
            j2 = points[(i + 1) % len(points)]
            nxt = points[(i + 2) % len(points)]
            join = float(np.linalg.norm(j2 - j1))
            outer = _unit(nxt - prev)
            d1 = _unit(j1 - prev)
            d2 = _unit(nxt - j2)
            if join > opts['endpoint_join'] or outer is None or d1 is None or d2 is None:
                continue
            par = np.sin(np.radians(opts['collinear_angle']))
            if (abs(float(np.cross(d1, outer))) > par or abs(float(np.cross(d2, outer))) > par
                    or float(np.dot(d1, outer)) < 0.9 or float(np.dot(d2, outer)) < 0.9):
                continue
            dist1 = abs((j1[0] - prev[0]) * outer[1] - (j1[1] - prev[1]) * outer[0])
            dist2 = abs((j2[0] - prev[0]) * outer[1] - (j2[1] - prev[1]) * outer[0])
            if dist1 > opts['collinear_dist'] or dist2 > opts['collinear_dist']:
                continue
            del points[i]
            del points[i % len(points)]
            changed = True
            break
    return np.asarray(points, np.float64)


# ── 候选生成 ────────────────────────────────────────────────────────

def _angle_fit(total_deg, opts):
    """
    两个内角加起来，像不像一条正经的接缝。

    直边接着直边 → 180°；直角拐过去 → 90°。差得越远分越低，超出容差就是 0 分。

    **注意这里只给分，不当否决条件**。对方那套是「两个端点都不合就判无效」，实测
    这条在本项目上不成立：自测用的那个「矩形切成 4 块」的正确摆法，三个接缝两端的内角
    之和分别是 149/76、104/121、121/59，一个 90 或 180 都没有——因为那些接缝的端点是
    **三块交汇**的地方，任意两块的内角之和本来就不会正好是 90 或 180（要算上第三块）。
    所以这里降级成排序依据，硬性条件改用下面那个几何上必须成立的 `_angle_possible`。
    """
    gap = min(abs(total_deg - 90.0), abs(total_deg - 180.0))
    if gap > opts['angle_tol']:
        return 0.0
    return opts['angle_tol'] - gap + 1.0


def _angle_possible(total_deg, opts):
    """
    两个内角加在一起**不能超过 180**（留点容差）。

    这一条是必须成立的：一个接缝端点上的材料，角度总和顶多凑满一个平角。
    两块自己的角加起来就超过 180，说明这一块压到邻居身上去了（另有第三块的话，
    第三块的角度还得是负数），几何上不可能。
    """
    return total_deg <= 180.0 + opts['angle_tol']


def _build_candidates(frontier, piece, edge_index, reverse, opts):
    """
    把这块的这条边贴到前沿那条边上。

    锚点（以哪一端为准对齐）由两端的角度分决定：哪端更像正经接缝就以哪端为准。
    两端**一样像**（或者都不像）时不猜——两种都生成，让后面的重叠检查和矩形验收去筛。
    猜错的代价是整块偏出去一个边长，而且再也纠不回来。
    """
    n = len(piece.verts)
    s_start = piece.verts[edge_index]
    s_end = piece.verts[(edge_index + 1) % n]
    s_start_angle = float(piece.angles[edge_index])
    s_end_angle = float(piece.angles[(edge_index + 1) % n])

    if reverse:
        # 把目标边**掉个头**：还是同一条线，片子的身子落到另一侧去。
        # 到底该正着贴还是反着贴，交给后面的重叠检查说话——这里两种都试
        t_start, t_end = frontier.b, frontier.a
        first = frontier.a_angle + s_end_angle
        second = frontier.b_angle + s_start_angle
        pairing = 'REVERSE'
    else:
        t_start, t_end = frontier.a, frontier.b
        first = frontier.a_angle + s_start_angle
        second = frontier.b_angle + s_end_angle
        pairing = 'DIRECT'

    # 几何上不可能的先扔掉：两端角加起来超过一个平角，说明这块压到邻居身上了
    if not (_angle_possible(first, opts) and _angle_possible(second, opts)):
        return ()

    f_score, s_score = _angle_fit(first, opts), _angle_fit(second, opts)
    if f_score > s_score:
        anchors = ('START',)
    elif s_score > f_score:
        anchors = ('END',)
    else:
        anchors = ('START', 'END')

    theta = _bearing(t_end - t_start) - _bearing(s_end - s_start)
    rot = _rot(theta)
    out = []
    for anchor in anchors:
        if anchor == 'START':
            translation = t_start - rot @ s_start
            other = rot @ s_end + translation
            other_target = t_end
            two_corner = s_score > 0.0
        else:
            translation = t_end - rot @ s_end
            other = rot @ s_start + translation
            other_target = t_start
            two_corner = f_score > 0.0
        # 另一端也得落到它该在的位置上。差太多说明这两条边其实不一样长
        other_err = float(np.linalg.norm(other - other_target))
        if other_err > opts['length_tol']:
            continue
        out.append(Candidate(Placed(piece, theta, translation), frontier, edge_index,
                             pairing, two_corner,
                             abs(frontier.length - float(piece.lengths[edge_index])),
                             other_err, f_score + s_score))
    return tuple(out)


def _candidate_is_valid(state, candidate, opts):
    """
    两条硬性检查：

      - 重叠：新块压在已经摆好的块上不行（留了余量，测量误差总归会让边角碰一碰）
      - 接触：光在角上蹭一下不算贴上，贴合处得有够长的一段

    只在候选的包围盒那一小块上算——整张掩膜逐像素与一遍太贵，而这里本来就是热路径。
    掩膜留的余量足够大（见 `_rasterize`），候选一定落在界内。
    """
    verts = candidate.placed.verts
    lo = verts.min(axis=0) - state.origin
    hi = verts.max(axis=0) - state.origin
    x0 = max(0, int(np.floor(lo[0])) - 1)
    y0 = max(0, int(np.floor(lo[1])) - 1)
    x1 = min(state.mask.shape[1], int(np.ceil(hi[0])) + 2)
    y1 = min(state.mask.shape[0], int(np.ceil(hi[1])) + 2)
    if x1 <= x0 or y1 <= y0:
        return False

    local = np.zeros((y1 - y0, x1 - x0), np.uint8)
    cv2.fillPoly(local, [np.rint(verts - state.origin - (x0, y0))
                         .astype(np.int32).reshape(-1, 1, 2)], 255)
    existing = state.mask[y0:y1, x0:x1]
    band = state.band[y0:y1, x0:x1]

    overlap = cv2.countNonZero(cv2.bitwise_and(existing, local))
    if state.area_px > 0 and overlap >= state.area_px * opts['max_overlap_ratio']:
        return False

    # 接触长度：把已摆部分往外膨一圈再和新块求交，交出来的像素数≈贴合处长度（像素），
    # 和下限（毫米换算过来的像素）比
    contact = cv2.countNonZero(cv2.bitwise_and(band, local))
    return contact >= opts['min_shared']


def _collect(state, opts):
    """把当前状态下**所有**能贴的候选都找出来，两端都对上的排前面"""
    two_corner, one_corner = [], []
    seen = set()
    for frontier in state.atomic + state.merged:
        for piece in state.remaining:
            for edge_index in range(len(piece.verts)):
                if abs(frontier.length - float(piece.lengths[edge_index])) > opts['length_tol']:
                    continue
                for reverse in (False, True):
                    for cand in _build_candidates(frontier, piece, edge_index, reverse, opts):
                        if not _candidate_is_valid(state, cand, opts):
                            continue
                        key = cand.pose_key()
                        if key in seen:
                            continue
                        seen.add(key)
                        (two_corner if cand.two_corner else one_corner).append(cand)

    def order(items):
        # 边长越接近越好，另一端落点越准越好，角度越正越好
        return sorted(items, key=lambda c: (c.length_err, c.other_err, -c.angle_score,
                                            c.placed.src.id, c.src_edge, c.pairing))

    ordered_two, ordered_one = order(two_corner), order(one_corner)
    if ordered_two:
        return ordered_two[0], tuple(ordered_two[1:] + ordered_one)
    if ordered_one:
        return ordered_one[0], tuple(ordered_one[1:])
    return None, ()


# ── 验收：到底是不是矩形 ────────────────────────────────────────────

def _rectangle_check(state, opts):
    """
    拼完了，验一遍：

      1. 把接缝的小豁口补上之后，**只有一坨**（拼成散开的好几堆就不算）
      2. 外轮廓只有一条
      3. 并集面积 / 最小外接矩形面积 ≥ 填充率下限

    没查「外轮廓正好 4 个顶点」：测量误差会让合并后的轮廓留下一两个几十像素的台阶，
    那样连真正的矩形都会被否掉（对方也在注释里踩过这个坑）。真正管用的是填充率。
    """
    r = opts['close_radius']
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (r * 2 + 1, r * 2 + 1))
    filled = cv2.morphologyEx(state.mask, cv2.MORPH_CLOSE, kernel)

    count, _ = cv2.connectedComponents(filled)
    if count != 2:                      # 0 是背景，前景该正好一坨
        return None
    contours, _ = cv2.findContours(filled, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if len(contours) != 1:
        return None

    (_c, (w, h), _a) = cv2.minAreaRect(contours[0])
    box = float(w) * float(h)
    if box <= 1.0:
        return None
    # 用并集本身的面积（含接缝的小孔），不是外轮廓面积
    fill = min(1.0, cv2.countNonZero(state.mask) / box)
    if fill <= opts['fill_ratio']:
        return None
    return fill, (float(w), float(h))


# ── 主搜索 ──────────────────────────────────────────────────────────

def assemble(pieces, px_per_mm=None, time_budget=None, node_log=None, params=None,
             length_tol_mm=None):
    """
    给几块碎片，找一种能拼成矩形的摆法。找不到就返回 None（**不退而求其次**）。

    pieces        : [Nx2 ndarray, ...] 每块的多边形，同一个坐标系（板面像素）
    px_per_mm     : 板面像素/毫米。容差都是毫米级的物理量，不换算没法用。
                    不给就按碎片总面积估一个——容差本来就是「相对拼图多大」才有意义
    length_tol_mm : 覆盖默认的边长容差。上层拿它从紧到松试几档，见 assembly.solve

    返回 {'placed': [(piece_id, verts, rotation_matrix), ...],
          'fill': 填充率, 'size': (宽, 高) 像素, 'nodes': 搜索节点数}
    """
    if pieces is None or not 2 <= len(pieces) <= 6:
        return None
    try:
        # 先洗干净再进搜索：一条直边中间多一个顶点就会让这条边谁都配不上（见 _clean_polygon）
        items = [Piece(i, _clean_polygon(p)) for i, p in enumerate(pieces)]
    except ValueError:
        return None

    if not px_per_mm or px_per_mm <= 0:
        total = sum(item.area for item in items)
        px_per_mm = float(np.sqrt(total) / 60.0) if total > 0 else 1.0
    opts = _make_opts(px_per_mm, params, length_tol_mm)

    # 每块有几条边，直接决定候选数（外轮廓边 × 剩余块 × 每块边数 × 2）。
    # 洗过之后还剩很多边，说明这块形状本来就复杂（或者识别出来的角点多）——
    # 说一声，别让人对着「拼不出来」猜
    edge_counts = [len(item.verts) for item in items]
    if node_log is not None and max(edge_counts) > 6:
        node_log.append(f'  注意：各块边数 {edge_counts}，偏多——候选会多不少，'
                        f'先看是不是轮廓没洗干净')

    ctx = {'nodes': 0,
           'deadline': time.perf_counter() + (time_budget or TIME_BUDGET_S),
           'opts': opts, 'failed': set()}

    def note(text):
        if node_log is not None:
            node_log.append('  ' + text)

    # 底块从大到小试：大块边多、约束强，先摆它更容易一次找对
    for seed in sorted(items, key=lambda p: p.area, reverse=True):
        ctx['failed'] = set()
        rest = tuple(p for p in items if p.id != seed.id)
        state = State([Placed(seed, 0.0, np.zeros(2))], rest, opts)
        note(f'边匹配：以 #{seed.id} 为底开始搜')
        result = _search(state, len(rest), ctx)
        if result is not None:
            fill, size = _rectangle_check(result, opts)
            note(f'边匹配：拼成 {size[0]:.1f}×{size[1]:.1f}px，填充 {fill:.4f}，'
                 f'搜了 {ctx["nodes"]} 个节点')
            return {
                'placed': [(item.src.id, item.verts, _rot(item.theta)) for item in result.placed],
                'fill': float(fill),
                'size': size,
                'nodes': ctx['nodes'],
            }
        note(f'边匹配：以 #{seed.id} 为底没搜出矩形')
    note(f'边匹配：怎么试都凑不成矩形（搜了 {ctx["nodes"]} 个节点）')
    return None


def _search(state, left, ctx):
    """
    递归摆下一块：先走「两端都对上」的首选，走死了再回头把这一层的候选挨个试一遍。

    失败过的**状态**记下来（和摆放顺序无关），等价的排列就不用再搜第二遍。
    """
    opts = ctx['opts']
    key = (left, state.key())
    if key in ctx['failed']:
        return None

    ctx['nodes'] += 1
    if ctx['nodes'] > MAX_NODES or time.perf_counter() > ctx['deadline']:
        ctx['failed'].add(key)
        return None

    if not state.remaining:
        return state if _rectangle_check(state, opts) else _fail(ctx, key)
    if left <= 0:
        return _fail(ctx, key)

    primary, backups = _collect(state, opts)
    if primary is None:
        return _fail(ctx, key)

    got = _search(_advance(state, primary, opts), left - 1, ctx)
    if got is not None:
        return got

    # 首选走死了：回到**当前这个状态**，把这一层的其他候选挨个试一遍。
    # 每次 _advance 都新建一份掩膜，所以这里天然就是干净的现场，没有回滚的坑
    primary_key = primary.pose_key()
    for cand in backups:
        if cand.pose_key() == primary_key:
            continue
        if time.perf_counter() > ctx['deadline'] or ctx['nodes'] > MAX_NODES:
            break
        got = _search(_advance(state, cand, opts), left - 1, ctx)
        if got is not None:
            return got
    return _fail(ctx, key)


def _fail(ctx, key):
    ctx['failed'].add(key)
    return None


def _advance(state, candidate, opts):
    remaining = tuple(p for p in state.remaining if p.id != candidate.placed.src.id)
    return State(state.placed + (candidate.placed,), remaining, opts)
