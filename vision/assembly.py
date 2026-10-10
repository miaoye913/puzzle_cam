"""
碎片拼接：把识别到的几块碎片摆成一个矩形。

思路参考了公开的 2026 电赛 E 题解法（NUEDC-2026-E，GPL-3.0）的**做法**：
不要「先猜一个摆法，再判断它是不是矩形」，而是**束搜索**——每步把所有能放的摆法都生成出来，
按「离矩形有多近」打分，只留下最好的若干条继续，最后取分最高的那条。
代码是自己写的，没有复制对方的实现。

原来的写法是「精确匹配 + 布尔判定」：要求两块碎片的边严格等长、拼完必须刚好是矩形，
否则整个分支扔掉。实测那样在真实数据上走不通——测量出来的碎片角度有几个度的误差、
边有几个像素的误差，精确闭合永远差一点点，于是**正确的摆法反而全被剪掉**，
搜索只能在一堆错误摆法里挑「最不难看的」，最好也只填满外接矩形的 85%。

改成打分之后，误差不再是「一票否决」，而是变成一个扣分项，正确摆法自然排在前面。

对齐方式也比原来多两种。原来只允许把一条边**端对端**贴上去；现在每条边对有三种摆法：
中点对齐、起点对终点、终点对起点。**中点对齐是原来完全没有的**——短边贴在长边正中间
这种接法，端对端根本表达不出来。
"""

import time

import numpy as np
import cv2

# 边长匹配的相对容差：两条边长度差超过它就不考虑贴在一起
EDGE_TOLERANCE = 0.45
# 每步保留多少条候选摆法。
#
# 实测（真实碎片，4 块）：beam 从 2000 一路降到 **5**，搜出来的尺寸、填充率、分数
# **完全一样**——说明真正有希望的分支很少，宽束只是白算。已知答案的合成剖分同理。
# 所以平时用窄束；窄束万一没搜出像样的矩形，再用宽束重试一遍兜底。
BEAM_WIDTH = 40
BEAM_WIDTH_WIDE = 400
# 差到这个地步才值得用宽束重搜一遍。窄束和宽束实测结果完全一致，宽束只是兜底，
# 而它一开就是十几倍的时间，所以门槛要卡得**很**低——卡高了反而会因为「不够好」
# 触发重搜，把速度又赔回去（这个坑踩过：门槛设 0.97，而 960x540 下最好只到 0.9675，
# 每次都白跑一遍宽束）。
BAD_FILL = 0.90
# 碎片先朝内缩几像素再拼——**这是量出来的，不是拍的**：
# 掩膜经过 HSV 阈值 + 形态学之后，碎片的边界整体比实物大 3px 左右（本机实测最优值），
# 不缩的话几块之间怎么摆都会留下缝。所以这里把内缩量也当成待定参数，用拼合结果自己选。
SHRINK_CANDIDATES = (0.0, 1.0, 2.0, 3.0, 4.0, 5.0)
# 重叠超过碎片自身面积的这个比例就判为摆法无效
OVERLAP_RATIO = 0.025
# 多个拉伸系数下都试一遍（见 solve），先试哪个由角度整齐度决定
ASPECT_RANGE = (0.95, 1.30, 0.01)
# 整个拼接的时间预算（秒）
TIME_BUDGET_S = 12.0


def _ccw(poly):
    p = np.asarray(poly, np.float64).reshape(-1, 2)
    if cv2.contourArea(p.reshape(-1, 1, 2).astype(np.float32), oriented=True) < 0:
        p = p[::-1]
    return p


def _area(poly):
    return abs(cv2.contourArea(np.asarray(poly, np.float32).reshape(-1, 1, 2)))


def _edge(poly, k):
    return poly[(k + 1) % len(poly)] - poly[k]


def _angles(poly):
    n = len(poly)
    out = []
    for i in range(n):
        v1 = poly[(i - 1) % n] - poly[i]
        v2 = poly[(i + 1) % n] - poly[i]
        c = np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2))
        out.append(float(np.degrees(np.arccos(np.clip(c, -1.0, 1.0)))))
    return out


def _rot(angle):
    c, s = np.cos(angle), np.sin(angle)
    return np.array([[c, -s], [s, c]], np.float64)


def _alignments(moving, moving_edge, fixed_world, fixed_edge):
    """
    把 moving 的第 moving_edge 条边贴到 fixed_world 的第 fixed_edge 条边上。

    反向共线（碎片落在另一侧），然后给三种偏移：
      - 中点对齐：两边中点重合，适合「一长一短、短的居中贴在长的上」
      - 起点对终点 / 终点对起点：两边的一端重合，适合「两块齐头」

    中点对齐这一种是关键：短边贴在长边中间时，光靠端对端永远表达不出来。
    """
    ms = moving[moving_edge]
    me = moving[(moving_edge + 1) % len(moving)]
    fs = fixed_world[fixed_edge]
    fe = fixed_world[(fixed_edge + 1) % len(fixed_world)]

    mv = me - ms
    fv = fs - fe                      # 反向，保证碎片在外侧
    if np.linalg.norm(mv) < 1e-9 or np.linalg.norm(fv) < 1e-9:
        return []
    rotation = _rot(np.arctan2(fv[1], fv[0]) - np.arctan2(mv[1], mv[0]))

    mmid = (ms + me) * 0.5
    fmid = (fs + fe) * 0.5
    candidates = [
        fmid - rotation @ mmid,       # 中点对齐
        fs - rotation @ me,           # moving 的终点贴 fixed 的起点
        fe - rotation @ ms,           # moving 的起点贴 fixed 的终点
    ]
    out = []
    for t in candidates:
        if not any(float(np.dot(t - o, t - o)) < 0.5 for o in out):
            out.append(t)
    return [(rotation, t) for t in out]


def _apply(poly, transform):
    rotation, translation = transform
    return poly @ rotation.T + translation


def _prep_overlap(poly):
    """
    重叠判定要反复用的东西预先算好：缩过一点的凸包 + 包围盒。

    缩 0.2% 是为了「共边」不被算成相交。**必须预先算**——这是热路径：
    每个候选摆法都要和已放置的每一块比一次，之前每次都在里面重算凸包。
    """
    hull = cv2.convexHull(np.asarray(poly, np.float32))
    c = hull.reshape(-1, 2).mean(axis=0)
    hull = (c + (hull.reshape(-1, 2) - c) * 0.998).reshape(-1, 1, 2).astype(np.float32)
    xs = hull.reshape(-1, 2)[:, 0]
    ys = hull.reshape(-1, 2)[:, 1]
    return hull, (float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max()))


def _overlap_ratio(moving, placed_prepared, moving_area):
    """和已放置的块相交面积占比。碎片近似凸，用 intersectConvexConvex 快得多。"""
    hull, (x0, y0, x1, y1) = moving
    for oh, (ox0, oy0, ox1, oy1) in placed_prepared:
        # 包围盒先粗筛：不相交就直接跳过，省下一次 intersectConvexConvex
        if x1 < ox0 or x0 > ox1 or y1 < oy0 or y0 > oy1:
            continue
        try:
            area, _ = cv2.intersectConvexConvex(hull, oh)
        except cv2.error:
            area = float('inf')
        if area > moving_area * OVERLAP_RATIO:
            return True
    return False


# ── 打分 ─────────────────────────────────────────────────────────────

def _bbox_area(polys):
    pts = np.vstack(polys).astype(np.float32)
    (_c, (w, h), _a) = cv2.minAreaRect(pts)
    return float(w) * float(h)


def _partial_cost(placed, match_error, areas):
    """搜索途中用的排序依据：贴合误差 + 外接矩形比碎块总面积大出多少。"""
    pieces_area = sum(areas)
    if pieces_area <= 0:
        return match_error
    compactness = max(0.0, _bbox_area(placed) / pieces_area - 1.0)
    return match_error + 0.08 * compactness


def _union_metrics(placed, scale=3.0):
    """栅格化并集：外轮廓顶点数 + 连通率（有没有拼成散开的好几坨）。"""
    pts = np.vstack(placed)
    mn = np.floor(pts.min(axis=0) - 3.0)
    mx = np.ceil(pts.max(axis=0) + 3.0)
    size = np.maximum(1, np.ceil((mx - mn) * scale)).astype(int)
    if size[0] * size[1] > 9_000_000:
        return 99, 0.0
    mask = np.zeros((size[1], size[0]), np.uint8)
    for p in placed:
        cv2.fillPoly(mask, [np.round((p - mn) * scale).astype(np.int32)], 255)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return 99, 0.0
    largest = max(contours, key=cv2.contourArea)
    outer = cv2.approxPolyDP(largest, cv2.arcLength(largest, True) * 0.015, True)
    total = sum(cv2.contourArea(c) for c in contours)
    connected = cv2.contourArea(largest) / max(1.0, total)
    return len(outer), float(connected)


def _final_score(placed, areas, target=None):
    """最终打分。越小越像矩形。不用「是不是矩形」这种一票否决的判据。"""
    pieces_area = sum(areas)
    bbox = _bbox_area(placed)
    if bbox <= 0 or pieces_area <= 0:
        return float('inf'), {}
    fill = pieces_area / bbox
    outer_vertices, connected = _union_metrics(placed)
    score = abs(1.0 - fill) * 3.0
    score += abs(outer_vertices - 4) * 0.20
    score += (1.0 - connected) * 2.0
    if target:
        (rw, rh) = sorted(target)
        pts = np.vstack(placed).astype(np.float32)
        (_c, (w, h), _a) = cv2.minAreaRect(pts)
        short, long = sorted((float(w), float(h)))
        score += (abs(short - rw) / rw + abs(long - rh) / rh) * 1.8
    return score, {'fill': fill, 'outer_vertices': outer_vertices, 'connected': connected}


# ── 束搜索 ───────────────────────────────────────────────────────────

def _beam_solve(polys, beam_width=BEAM_WIDTH, deadline=None):
    """
    束搜索：每步展开所有「把某块未放的碎片贴到某块已放碎片某条边上」的摆法，
    按代价排序只留前 beam_width 条，最后取分最高的。

    和「深度优先找到第一个解就返回」的区别在于：错误的分支不会因为一次不完美的贴合
    就被整体砍掉，而是排在后面——真实数据里有测量误差，这点是决定性的。
    """
    n = len(polys)
    if not 2 <= n <= 6:
        return None
    areas = [_area(p) for p in polys]
    lengths = [np.array([np.linalg.norm(_edge(p, k)) for k in range(len(p))])
               for p in polys]
    anchor = int(np.argmax(areas))

    # state = (transforms dict, match_error)
    states = [({anchor: (np.eye(2), np.zeros(2))}, 0.0)]
    placed_count = 1

    while placed_count < n:
        expanded = []
        for transforms, err in states:
            if deadline is not None and time.perf_counter() > deadline:
                return None
            placed_worlds = {i: _apply(polys[i], t) for i, t in transforms.items()}
            # 已放置那几块的凸包只和 state 有关，和候选无关——在这里算一次，
            # 别放到逐个候选的循环里重算
            placed_prepared = [_prep_overlap(w) for w in placed_worlds.values()]
            for moving in range(n):
                if moving in transforms:
                    continue
                moving_poly = polys[moving]
                for fixed, fixed_world in placed_worlds.items():
                    for fe in range(len(polys[fixed])):
                        fl = float(lengths[fixed][fe])
                        for me in range(len(moving_poly)):
                            ml = float(lengths[moving][me])
                            rel = abs(fl - ml) / max(fl, ml)
                            if rel > EDGE_TOLERANCE:
                                continue
                            for transform in _alignments(moving_poly, me, fixed_world, fe):
                                world = _apply(moving_poly, transform)
                                if _overlap_ratio(_prep_overlap(world), placed_prepared,
                                                  areas[moving]):
                                    continue
                                new_t = dict(transforms)
                                new_t[moving] = transform
                                new_err = err + rel * 0.15
                                expanded.append((new_t, new_err))
        if not expanded:
            return None
        # 去重：同样的变换签名只留代价最小的那条
        dedup = {}
        for transforms, err in expanded:
            key = tuple(sorted((i, round(float(t[1][0]), 1), round(float(t[1][1]), 1),
                                round(float(t[0][0, 0]), 4), round(float(t[0][1, 0]), 4))
                               for i, t in transforms.items()))
            if key not in dedup or err < dedup[key][1]:
                dedup[key] = (transforms, err)
        expanded = list(dedup.values())
        expanded.sort(key=lambda s: _partial_cost(
            [_apply(polys[i], t) for i, t in s[0].items()], s[1],
            [areas[i] for i in s[0]]))
        states = expanded[:beam_width]
        placed_count += 1

    best = None
    for transforms, err in states:
        # ids 必须和 placed **同序**：transforms 是按「先放基准块、再放别的」的插入顺序，
        # 而基准块是面积最大的那块，未必是 0 号——用 sorted(transforms) 会错位，
        # 表现就是左边拼好的矩形里，某一块的形状和右边它标的编号对不上
        ids = list(transforms)
        placed = [_apply(polys[i], t) for i, t in transforms.items()]
        score, metrics = _final_score(placed, [areas[i] for i in transforms])
        if best is None or score < best[0]:
            best = (score, metrics, ids, [np.asarray(x, np.float64) for x in placed])
    return best


def _normalize(placements):
    """摆正并平移到原点，前端拿到就能直接按比例画。"""
    allp = np.vstack(placements)
    (_c, (_w, _h), ang) = cv2.minAreaRect(allp.astype(np.float32))
    rad = np.radians(ang)
    rot = np.array([[np.cos(rad), np.sin(rad)], [-np.sin(rad), np.cos(rad)]])
    out = [p @ rot.T for p in placements]
    allp = np.vstack(out)
    mn = allp.min(axis=0)
    out = [p - mn for p in out]
    allp = np.vstack(out)
    return out, [float(allp[:, 0].max()), float(allp[:, 1].max())]


def _scaled(polys, s):
    out = []
    for p in polys:
        c = p[:, 1].mean()
        q = p.copy()
        q[:, 1] = (q[:, 1] - c) * s + c
        out.append(q)
    return out


def _shrink_poly(poly, d):
    """把多边形每条边朝内部平移 d，顶点取相邻两条平移线的交点。d=0 原样返回。"""
    if d <= 0:
        return poly
    n = len(poly)
    lines = []
    for i in range(n):
        a = poly[i]
        b = poly[(i + 1) % n]
        v = b - a
        length = np.linalg.norm(v)
        if length < 1e-9:
            return poly
        normal = np.array([-v[1], v[0]]) / length     # 内法向
        lines.append((a + normal * d, v / length))
    out = []
    for i in range(n):
        p0, u = lines[(i - 1) % n]
        p1, v = lines[i]
        cross = u[0] * v[1] - u[1] * v[0]
        if abs(cross) < 1e-9:
            out.append(poly[i])
            continue
        t = ((p1[0] - p0[0]) * v[1] - (p1[1] - p0[1]) * v[0]) / cross
        out.append(p0 + t * u)
    return np.array(out, np.float64)


def solve(pieces, aspect=None, shrink=None, beam_width=None, time_budget=None, node_log=None):
    """
    把碎片拼成矩形。返回最接近矩形的那组摆法（附打分），拼不出来返回 None。

    pieces: [Nx2 的 ndarray, ...]，每块的多边形，同一坐标系。

    注意返回的是**搜索到的最优摆法**，不是「经过证明的完美矩形」——真实数据有测量误差，
    要求完美闭合等于永远搜不到。判断质量看返回里的 fill / outer_vertices / score。
    """
    polys = [_ccw(p) for p in pieces]
    if not 2 <= len(polys) <= 6:
        return None
    budget = time_budget or TIME_BUDGET_S
    deadline = time.perf_counter() + budget
    width = beam_width or BEAM_WIDTH

    # 默认就用矫正后的原始比例。早先以为要一起搜拉伸系数，那是被「精确匹配」的搜索方式
    # 逼出来的——打分式的束搜索在 s=1.0 上直接就把 4 个外轮廓顶点的摆法找出来了。
    # 要扫一圈的话显式传 aspect=[...]，代价是每个系数多花几秒。
    aspects = [1.0] if aspect is None else list(aspect)

    def scan(beam_width):
        """按 (拉伸, 内缩) 扫一遍，返回 (最好的结果, 是否超时)。搜到像样的矩形就早退。"""
        best = None
        for s in aspects:
            for d in (shrink if shrink is not None else SHRINK_CANDIDATES):
                if time.perf_counter() > deadline:
                    return best, True
                shrunk = [_shrink_poly(p, d) for p in polys]
                got = _beam_solve(_scaled(shrunk, s), beam_width, deadline)
                if got is None:
                    continue
                score, metrics, ids, placed = got
                placements, size = _normalize(placed)
                cand = {
                    'size': [round(size[0], 1), round(size[1], 1)],
                    'aspect': round(float(s), 3),
                    'shrink_px': round(float(d), 1),
                    'score': round(float(score), 4),
                    'fill': round(float(metrics.get('fill', 0.0)), 4),
                    'outer_vertices': int(metrics.get('outer_vertices', 0)),
                    'placements': [{'index': int(ids[i]),
                                    'polygon': [[round(float(x), 1), round(float(y), 1)] for x, y in p]}
                                   for i, p in enumerate(placements)],
                }
                if node_log is not None:
                    node_log.append(
                        f'  拉伸 {s:.2f} 内缩 {d:.0f}px: 填满外接矩形 {cand["fill"]:.4f}，'
                        f'外轮廓 {cand["outer_vertices"]} 个顶点，总分 {cand["score"]:.4f}')
                if best is None or cand['score'] < best['score']:
                    best = cand
        return best, False

    # 窄束扫一遍就够了——实测它和宽束结果完全一致。不设「够好了就早退」那类门槛：
    # 填充率随内缩量是先升后降的，看到 0.96 就停会停在坡上，拿到的是次优解。
    best, timed_out = scan(width or BEAM_WIDTH)

    # 只有结果明显不成样子时才放宽重搜。这一步贵十几倍，宁可用窄束的次优解也不轻易走
    if (not timed_out and time.perf_counter() < deadline
            and (best is None or best['fill'] < BAD_FILL)):
        if node_log is not None:
            node_log.append(f'  窄束结果太差（{0 if best is None else best["fill"]:.4f}），'
                            f'放宽到 {BEAM_WIDTH_WIDE} 再搜一遍')
        wide, _ = scan(BEAM_WIDTH_WIDE)
        if wide is not None and (best is None or wide['score'] < best['score']):
            best = wide
    return best
