"""
碎片拼接：把识别到的几块碎片摆成一个矩形。

**算法本体在 `assembly_edge.py`**（边长 + 角度匹配 + 回溯 + 矩形验收，凑不出矩形就返回
None）。这个文件只做两件事：按「边长容差从紧到松」试几档把它跑起来，以及把结果整理成
前端要的形状。

以前这里还有一套「束搜索 + 打分」。它的毛病在打分函数上：「离矩形有多近」只是**一项扣分**，
摆错的方案照样能排到前面，所以它**总能交出一个东西来，而那个东西未必是矩形**——错的结果
会被画到屏幕上、机器还会照着摆。现在删掉了：拼不出来就返回 None，让上层把原因显示出来，
而不是拿一个「看着像」的糊弄过去。

> 中间留过一版「严的先上、旧的后备」，结果界面上「凑合」和「拼对了」长得几乎一样，
> 谁都可能照着摆。宁可空着——空着至少会告诉你为什么（见 pipeline 的 `assembly_log`）。
"""

import time

import numpy as np
import cv2

import assembly_edge

# 一次拼接的时间预算（秒）。边匹配正常几百毫秒就出结果，这个上限是给「怎么都拼不出来」
# 那种情况兜底的——超时会记进日志，和「搜遍了没有」分得开
TIME_BUDGET_S = 12.0
# 边长容差从紧到松试几档，先成的那档作数：容差越紧，配上的越可信。
# 实测紧于 3mm 一条都配不上（掩膜边界比实物大 3px 左右，而相邻两块的角不一样，
# 膨胀出来的长度差不相同，误差就在 3mm 这个量级）
EDGE_TOL_LADDER_MM = (3.0, 5.0, 8.0)


def _ccw(poly):
    p = np.asarray(poly, np.float64).reshape(-1, 2)
    if cv2.contourArea(p.reshape(-1, 1, 2).astype(np.float32), oriented=True) < 0:
        p = p[::-1]
    return p


def _area(poly):
    return abs(cv2.contourArea(np.asarray(poly, np.float32).reshape(-1, 1, 2)))


def _bbox_area(polys):
    pts = np.vstack(polys).astype(np.float32)
    (_c, (w, h), _a) = cv2.minAreaRect(pts)
    return float(w) * float(h)


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


def _normalize(placements):
    """
    摆正并平移到原点，前端拿到就能直接按比例画。

    同时把这里施加的**全局旋转**返回出去：每块最终相对它自己原始形状转了多少度，
    要靠它和「求解时那一块自己的旋转」复合出来（见 solve）。
    """
    allp = np.vstack(placements)
    (_c, (_w, _h), ang) = cv2.minAreaRect(allp.astype(np.float32))
    rad = np.radians(ang)
    rot = np.array([[np.cos(rad), np.sin(rad)], [-np.sin(rad), np.cos(rad)]])
    out = [p @ rot.T for p in placements]
    allp = np.vstack(out)
    mn = allp.min(axis=0)
    out = [p - mn for p in out]
    allp = np.vstack(out)
    return out, [float(allp[:, 0].max()), float(allp[:, 1].max())], rot


def _solve_by_edges(polys, board_w_px, budget, node_log):
    """
    边匹配那一套（见 assembly_edge）。**边长容差从紧到松试几档**，先成的那档就作数：
    容差越紧配出来的摆法越可信，所以宁可先试紧的。

    返回的字典就是前端要的形状（见 README 里结果结构那节）。
    """
    px_per_mm = (board_w_px / assembly_edge.BOARD_W_MM) if board_w_px else None
    overall = time.perf_counter() + budget
    if not 2 <= len(polys) <= 6:
        if node_log is not None:
            node_log.append(f'  边匹配：{len(polys)} 块不在 2~6 之间，这一套不适用')
        return None
    got = None
    for tol_mm in EDGE_TOL_LADDER_MM:
        if time.perf_counter() > overall:
            break
        got = assembly_edge.assemble(
            [np.asarray(p, np.float64) for p in polys],
            px_per_mm=px_per_mm,
            # 每一档单独限时：三档加起来别超过整个预算的一半多。正常情况第一档
            # 几百毫秒就出结果了，这个上限是给「怎么都拼不出来」那种情况兜底的
            time_budget=max(1.5, min(budget / 4.0, 3.0)),
            node_log=node_log,
            length_tol_mm=tol_mm,
        )
        if got is not None:
            if node_log is not None:
                rung_note = '（这一轮被时间砍过，结果可能不是它搜得动的最好的）' if got.get('aborted') else ''
                node_log.append(f'  边匹配：边长容差 {tol_mm:.1f}mm 这档成了{rung_note}')
            break
    if got is None:
        if node_log is not None:
            rungs = '/'.join(f'{t:.0f}' for t in EDGE_TOL_LADDER_MM)
            node_log.append(f'  边匹配：边长容差 {rungs}mm 三档都没凑成矩形'
                            f'（碎片可能是量得太不准，或者这几块本来就不拼这个矩形）')
        return None

    ids = [pid for pid, _v, _r in got['placed']]
    placed = [np.asarray(v, np.float64) for _p, v, _r in got['placed']]
    rots = [np.asarray(r, np.float64) for _p, _v, r in got['placed']]
    placements, size, norm_rot = _normalize(placed)

    cand = {
        'size': [round(size[0], 1), round(size[1], 1)],
        'placements': [{
            'index': int(ids[i]),
            'polygon': [[round(float(x), 1), round(float(y), 1)] for x, y in placements[i]],
            # 这块相对它自己原始形状一共要转多少度 = 求解时的旋转 ∘ 摆正时的旋转
            'rot_deg': round(float(np.degrees(np.arctan2(
                (norm_rot @ rots[i])[1, 0], (norm_rot @ rots[i])[0, 0]))), 1),
        } for i in range(len(placements))],
    }
    # 打分只是给人和日志看的参考值（越小越像矩形），不参与任何取舍
    score, metrics = _final_score(placements, [_area(p) for p in placements])
    cand['score'] = round(float(score), 4)
    cand['fill'] = round(float(metrics.get('fill', 0.0)), 4)
    cand['outer_vertices'] = int(metrics.get('outer_vertices', 0))
    return cand


def solve(pieces, time_budget=None, node_log=None, board_w_px=None):
    """
    把碎片拼成矩形。**拼不出来返回 None**——这条路上没有兜底。

    pieces:     [Nx2 的 ndarray, ...]，每块的多边形，同一坐标系（板面像素）
    board_w_px: 板面在矫正图里有多宽（像素）。边匹配的容差是毫米级的物理量，
                要靠它折算；不给就按碎片总面积估一个，精度差些但能用
    node_log:   传一个 list 进来，搜索过程会往里写。**拼不出来时这几行就是原因**，
                pipeline 会把它放进结果、显示到界面上
    """
    return _solve_by_edges([_ccw(p) for p in pieces], board_w_px,
                           time_budget or TIME_BUDGET_S, node_log)
