"""
拼板视觉分析：定位绿色拼板 → **裁剪并矫正**成正面视图 → 在矫正后的板面上找白色块和线条。

设计约束（为了三个平台都能跑）：
  - 只依赖 opencv-python + numpy，没有任何硬件/系统相关的调用
  - 输入是一张 BGR 图（np.ndarray），输出是纯 JSON 可序列化的 dict
  - analyze() 是唯一的入口，想把算法换成自己的，改这一个函数即可

坐标一律用**原图像素坐标**返回（矫正空间里算出来的结果会映射回去），前端按显示尺寸缩放。
"""

import time

import cv2
import numpy as np

import assembly

# ── 阈值：都在 HSV 空间，改这里就能适配不同的灯光/拼板颜色 ──────────────
# 绿色拼板：色相 35~85 覆盖黄绿到青绿
BOARD_HSV_LOW = np.array([35, 80, 80], np.uint8)
BOARD_HSV_HIGH = np.array([85, 255, 255], np.uint8)
# 白色块。注意：这些块在实物上并不是纯白，而是带蓝的浅色——
# 实测 BGR(230,207,175) → H≈102 S≈61~63 V≈224~230，蓝通道比红高 55。
# 所以饱和度上限必须给到 95（曾卡在 60，整批块被拒，只剩最亮的一小块残片）。
# 下限 V=185：绿板 S≈207 被排除，地板 V≈163 被排除。
PIECE_HSV_LOW = np.array([0, 0, 185], np.uint8)
PIECE_HSV_HIGH = np.array([180, 95, 255], np.uint8)

# ── 面积门槛（都按比例给，和输入分辨率无关）────────────────────────────
MIN_BOARD_RATIO = 0.02        # 拼板小于画面 2% 就当没找到
MIN_PIECE_BOARD_RATIO = 0.002  # 单块小于板面积 0.2% 视为噪点

KERNEL = np.ones((5, 5), np.uint8)

# ── 裁剪矫正 ─────────────────────────────────────────────────────────
# 矫正后的目标尺寸按检测到的四条边平均长度来取（分辨率不变，阈值不必跟着改）。
# 已知实物长宽比的话填在这里（比如 4/3），斜着拍也不会被压扁；
# None = 用画面里量到的平均边长，是斜视角下的一个近似。
BOARD_REAL_ASPECT = None
WARP_MIN = (160, 120)
WARP_MAX = (800, 600)
BOARD_INSET = 4               # 矫正后往里收几像素，避开插值出来的边缘过渡带

# 分开粘连块时最多腐蚀几次。腐蚀量不是写死的：见 split_pieces()，
# 它会逐步加大腐蚀量直到连通域数不再增加，因此对输入分辨率不敏感。
SPLIT_ERODE_MAX = 8

# ── 多边形简化（见 simplify_polygon 的说明）──────────────────────────
# 初筛 epsilon，相对周长。给得比较粗是有意的：先把「七巧板就那几条边」这件事定下来，
# 被削掉的尖角随后由直线重新拟合补回来。七巧板最短的边也在周长的 15% 以上，
# 所以 3% 这个量级不会把真边吃掉。
POLY_EPS_RATIO = 0.030
COLLINEAR_DEG = 160.0         # 内角大于它 = 落在直边上的采样抖动，删掉
REFIT_PASSES = 4              # 直线重拟合的迭代次数
SHORT_EDGE_RATIO = 0.25       # 短边相对邻边低于它，才考虑合并（见 _merge_insignificant_edges）
TINY_AREA_RATIO = 0.015       # 去掉这条边造成的面积变化低于整块的这个比例，就认为它不承载形状

# ── 板上的线条 ───────────────────────────────────────────────────────
# 判据是「和板面主色不同」，不认颜色本身：红线、黑线、深色接缝都抓得到。
# 刻意不做 MORPH_OPEN：那会把 1~3 像素宽的细线直接磨掉，正是要找的东西。
# 噪声靠「长度 + 长宽比 + 面积下限」三条一起卡。
LINE_MIN_BOARD_RATIO = 0.0015  # 线面积小于板面积 0.15% 忽略（滤掉 JPEG 噪点）
LINE_MIN_LENGTH_RATIO = 0.25   # 线长小于板对角线这个比例算噪声
LINE_MIN_ELONGATION = 3.5      # 长宽比，低于它算块不算线
LINE_MAX_WIDTH = 12            # 矫正后板面坐标下的最大线宽(px)
LINE_DEV_H = 22                # 色相偏离板面主色多少算「不一样」
LINE_DEV_S = 75
LINE_DEV_V = 45

# ── 碎片拼接 ─────────────────────────────────────────────────────────
# 把识别到的碎片摆成一个矩形，结果放在 assemble() 返回的 'assembly' 里。
# 识别是一次性的（点一下按钮算一帧），所以这里算得慢一点无所谓，不占持续开销。
ASSEMBLY_ENABLE = True
ASSEMBLY_MAX_PIECES = 8        # 超过这个块数就不拼了：搜索是指数级的
ASSEMBLY_TIME_BUDGET = 25.0    # 单次拼接的时间上限（秒）；超时就报「没拼出来」


def order_corners(points):
    """把任意顺序的四个点整理成 左上 / 右上 / 右下 / 左下（图像坐标系，y 向下）。"""
    pts = np.asarray(points, dtype=np.float32).reshape(-1, 2)
    if len(pts) != 4:
        return None
    ordered = np.zeros((4, 2), np.float32)
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    ordered[0] = pts[np.argmin(s)]   # 左上：x+y 最小
    ordered[2] = pts[np.argmax(s)]   # 右下：x+y 最大
    ordered[1] = pts[np.argmin(d)]   # 右上：y-x 最小
    ordered[3] = pts[np.argmax(d)]   # 左下：y-x 最大
    return ordered


def find_board(hsv, shape):
    """找绿色拼板，返回 (四角点, 板面掩膜, 板轮廓)；找不到返回 (None, None, None)。"""
    h, w = shape[:2]
    mask = cv2.inRange(hsv, BOARD_HSV_LOW, BOARD_HSV_HIGH)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, KERNEL)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, KERNEL)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None, None, None

    biggest = max(contours, key=cv2.contourArea)
    if cv2.contourArea(biggest) < MIN_BOARD_RATIO * h * w:
        return None, None, None

    # 板面掩膜用原始外轮廓填充，白块挖出的洞会被补上
    board_mask = np.zeros((h, w), np.uint8)
    cv2.drawContours(board_mask, [biggest], -1, 255, -1)

    perimeter = cv2.arcLength(biggest, True)
    for eps_ratio in (0.02, 0.03, 0.05, 0.08):
        approx = cv2.approxPolyDP(biggest, eps_ratio * perimeter, True)
        if len(approx) == 4:
            return order_corners(approx), board_mask, biggest

    # 点数凑不出 4 个时退回最小外接矩形，至少保证有可用的参考框
    rect = cv2.minAreaRect(biggest)
    return order_corners(cv2.boxPoints(rect)), board_mask, biggest


def board_bow(corners, contour):
    """
    量一下板子的边有多弯——用来判断画面里有没有需要修的镜头畸变。

    做法：沿轮廓走「两个角点之间那一段弧」，量它离两点连线的最大垂直偏移。
    这一点不能靠全部轮廓点投影去算：矩形的对边点也会投到同一条边的范围内，
    那样量出来的偏移是整块板的宽度（实测 258px / 边长 375px），完全没意义。

    透视矫正（warp）修不掉镜头畸变，所以这里只报数据不猜模型：真要做畸变矫正需要
    标定（棋盘格）拿到内参，或者用直线度做一维搜索估一个径向系数。
    实测本机的板边偏移是 0.5%~1.1%，且四条边同向，属于掩膜边缘抗锯齿的量级，无需矫正。
    """
    if contour is None:
        return {'max_px': 0.0, 'ratio': 0.0}
    pts = contour.reshape(-1, 2).astype(float)
    n = len(pts)
    if n < 8:
        return {'max_px': 0.0, 'ratio': 0.0}

    idx = [int(np.argmin(np.linalg.norm(pts - pt, axis=1))) for pt in corners]
    worst = 0.0
    for i in range(4):
        a_idx, b_idx = idx[i], idx[(i + 1) % 4]
        fwd = [(a_idx + k) % n for k in range((b_idx - a_idx) % n + 1)]
        bwd = [(a_idx - k) % n for k in range((a_idx - b_idx) % n + 1)]
        arc = pts[fwd if len(fwd) <= len(bwd) else bwd]

        a, b = corners[i], corners[(i + 1) % 4]
        ab = b - a
        length = float(np.linalg.norm(ab))
        if length < 8:
            continue
        u = ab / length
        normal = np.array([-u[1], u[0]])
        t = (arc - a) @ u
        inside = (t > 0.05 * length) & (t < 0.95 * length)
        if inside.sum() < 3:
            continue
        worst = max(worst, float(np.abs((arc[inside] - a) @ normal).max()))
    return {'max_px': round(worst, 2), 'ratio': round(worst / float(np.linalg.norm(corners[1] - corners[0])), 4)}


def rectify_board(frame, corners):
    """按四个角点把板面裁出来并矫正成正视图，返回 (矫正图, 正变换 H, 尺寸)。"""
    tl, tr, br, bl = corners
    width = (np.linalg.norm(tr - tl) + np.linalg.norm(br - bl)) / 2.0
    height = (np.linalg.norm(bl - tl) + np.linalg.norm(br - tr)) / 2.0
    if BOARD_REAL_ASPECT:
        height = width / BOARD_REAL_ASPECT
    w = int(np.clip(round(width), WARP_MIN[0], WARP_MAX[0]))
    h = int(np.clip(round(height), WARP_MIN[1], WARP_MAX[1]))

    dst = np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], np.float32)
    H = cv2.getPerspectiveTransform(corners.astype(np.float32), dst)
    warped = cv2.warpPerspective(frame, H, (w, h), flags=cv2.INTER_LINEAR)
    return warped, H, (w, h)


def _interior_angles(poly):
    """多边形每个顶点的内角（度）。"""
    n = len(poly)
    out = np.zeros(n)
    for i in range(n):
        a, b, c = poly[(i - 1) % n], poly[i], poly[(i + 1) % n]
        v1, v2 = a - b, c - b
        scale = np.linalg.norm(v1) * np.linalg.norm(v2)
        out[i] = 180.0 if scale < 1e-9 else np.degrees(np.arccos(np.clip(np.dot(v1, v2) / scale, -1.0, 1.0)))
    return out


def _line_intersection(p, u, q, v):
    """直线 p+t·u 与 q+s·v 的交点；近乎平行返回 None。"""
    cross = u[0] * v[1] - u[1] * v[0]
    if abs(cross) < 1e-9:
        return None
    t = ((q[0] - p[0]) * v[1] - (q[1] - p[1]) * v[0]) / cross
    return p + t * u


def _fit_edges(points, poly):
    """
    把轮廓点按「离哪条边最近」分组，对每条边做一次直线拟合。

    点按整条边分给最近的边，而不是只取两端附近：一条边上的采样点越多，
    拟合出来的方向越不受抗锯齿抖动影响，角点也就越准。
    """
    a = poly
    b = np.roll(poly, -1, axis=0)
    seg = b - a
    seg_len = np.linalg.norm(seg, axis=1)
    seg_len[seg_len < 1e-6] = 1e-6

    rel = points[:, None, :] - a[None, :, :]
    # 点到各条边所在直线的垂直距离
    cross = np.abs(rel[:, :, 0] * seg[None, :, 1] - rel[:, :, 1] * seg[None, :, 0]) / seg_len[None, :]
    owner = np.argmin(cross, axis=1)

    fitted = []
    for k in range(len(poly)):
        selected = points[owner == k]
        if len(selected) < 3:
            fitted.append(None)
            continue
        vx, vy, x0, y0 = cv2.fitLine(selected.astype(np.float32), cv2.DIST_L2, 0, 0.01, 0.01).ravel()
        fitted.append((np.array([x0, y0], np.float32), np.array([vx, vy], np.float32)))
    return fitted


def _refit_corners(poly, fitted):
    """每个角点 = 它两侧两条边的拟合直线求交。"""
    edges = np.linalg.norm(np.roll(poly, -1, axis=0) - poly, axis=1)
    out = []
    for i in range(len(poly)):
        prev, cur = fitted[i - 1], fitted[i]
        if prev is None or cur is None:
            out.append(poly[i])
            continue
        corner = _line_intersection(prev[0], prev[1], cur[0], cur[1])
        # 两条边近乎平行时交点会飞出去，这时保留原顶点
        limit = 0.35 * max(edges[i - 1], edges[i])
        if corner is None or not np.all(np.isfinite(corner)) or np.linalg.norm(corner - poly[i]) > limit:
            out.append(poly[i])
            continue
        out.append(corner)
    return np.array(out, np.float32)


def _drop_collinear(poly):
    """删掉内角接近 180° 的顶点（落在直边上的采样抖动）。真角点离 160° 很远，不会误伤。"""
    for _ in range(4):
        angles = _interior_angles(poly)
        collinear = np.where(angles > COLLINEAR_DEG)[0]
        if not len(collinear) or len(poly) - len(collinear) < 3:
            break
        poly = np.delete(poly, collinear, axis=0)
    return poly


def _merge_insignificant_edges(poly):
    """
    合并「不承载形状」的超短边。

    一个真角点被噪声或圆角弄出两个顶点时，两个内角往往在 110°~135° 之间——都不够
    「共线」（删不掉），于是这个角就被报成了两个。实测 JPEG 质量 80 下，一块拼图的
    左侧角被拆成相距 7px 的两个顶点（111° + 133°），而不经 JPEG 的同一帧只有 1 个。

    判据不看角度，看**这条边去掉了会不会改变形状**：把这条短边换成一个点（两侧边延长线的
    交点），看由此产生/消失的那块三角形面积占整块多大。真边（比如梯形那条短平行边）
    一去掉面积立刻变化一大截；噪声造出来的假边去掉后几乎无变化。
    """
    poly = poly.astype(np.float32)
    for _ in range(4):
        n = len(poly)
        if n <= 3:
            break
        edges = np.linalg.norm(np.roll(poly, -1, axis=0) - poly, axis=1)
        area = abs(cv2.contourArea(poly.reshape(-1, 1, 2)))
        if area <= 0:
            break

        drop = None
        for i in range(n):
            prev, nxt = (i - 1) % n, (i + 1) % n
            if edges[i] > SHORT_EDGE_RATIO * min(edges[prev], edges[nxt]):
                continue
            u = poly[i] - poly[prev]
            v = poly[(i + 2) % n] - poly[(i + 1) % n]
            u = u / np.linalg.norm(u)
            v = v / np.linalg.norm(v)
            hit = _line_intersection(poly[prev], u, poly[(i + 1) % n], v)
            if hit is None:
                continue
            triangle = np.array([poly[i], hit, poly[(i + 1) % n]], np.float32).reshape(-1, 1, 2)
            if abs(cv2.contourArea(triangle)) < TINY_AREA_RATIO * area:
                drop = (i, hit)
                break
        if drop is None:
            break
        index, hit = drop
        poly = poly[[k for k in range(n) if k not in (index, (index + 1) % n)]]
        poly = np.insert(poly, index % len(poly), hit, axis=0)
    return poly


def simplify_polygon(contour):
    """
    把轮廓压成「真正的角点」，重点是别把锐角拆散、也别把它磨圆。

    单纯调小 approxPolyDP 的 epsilon 解决不了，反而更糟：实物的尖角在掩膜里本来就有
    2~3 像素被磨圆或削平（抗锯齿 + 形态学），尖角一圈会出现一串拐得很浅的顶点，
    它们各自偏离弦的量都超过了统一 epsilon，于是被原样留下来。实测一块尖角拼图因此
    报出 7 个顶点，内角 90/163/96/151/130/142/128 度——肉眼就是一个锐角被切成了好几个角。

    所以走「先粗后精」：
      1) 用较粗的 epsilon 做一次 approxPolyDP，先把「这块图形大概有几条边」定下来；
         尖角在这一步会被削掉一块，这是可以接受的。
      2) 反复做「直线重拟合」：把轮廓点按最近的边分组、每条边拟合一条直线、
         角点取两侧拟合直线的交点。尖角就是这样被还原的——被削掉的那一小块
         仍然分别属于两条边，两条边的延长线一交，尖点就回来了。
      3) 收尾删掉近乎共线的顶点（内角 > COLLINEAR_DEG），并合并不承载形状的超短边
         （见 _merge_insignificant_edges：真角点被噪声拆成两个时，两个内角都不到共线
         的程度，光靠第 3 步的前半段删不掉）。

    安全阀拿**轮廓本身的面积**当基准，而不是拿上一轮的面积：重拟合把第 1 步削掉的尖角
    补回来，面积本来就是变大的（实测一块三角形 1618→1962，+21%），拿「变化超过 15%」
    当判据会把这个增长当成分组错误，反而退回到那个圆角三角形——
    等于在最需要重拟合的块上把重拟合关掉。真正该拦的是退化（顶点飞掉、多边形自交），
    所以只要求结果落在轮廓面积的合理区间内。
    """
    perimeter = cv2.arcLength(contour, True)
    if perimeter <= 0:
        return contour.reshape(-1, 2).astype(float)
    points = contour.reshape(-1, 2).astype(np.float32)
    contour_area = abs(cv2.contourArea(contour))
    poly = cv2.approxPolyDP(contour, POLY_EPS_RATIO * perimeter, True).reshape(-1, 2).astype(np.float32)

    for _ in range(REFIT_PASSES):
        refitted = _refit_corners(poly, _fit_edges(points, poly))
        after = abs(cv2.contourArea(refitted.reshape(-1, 1, 2).astype(np.float32)))
        # 轮廓在掩膜里的角是圆过的，所以还原尖角后略大于轮廓面积是对的；超出这个区间
        # 才说明有顶点飞了或自交了，退回上一轮
        if contour_area > 0 and not (0.70 * contour_area <= after <= 1.30 * contour_area):
            break
        settled = np.allclose(refitted, poly, atol=0.05)
        poly = refitted
        if settled:
            break

    # 收尾：先删共线点，再合并不承载形状的超短边；合并可能又造出共线点，所以再来一轮
    return _drop_collinear(_merge_insignificant_edges(_drop_collinear(poly)))


def _seeds_for_split(mask, min_area):
    """
    找「每块一个种子」所需的腐蚀量。

    逐步加大腐蚀，记录连通域数量；数量一旦回落就说明已经腐蚀过头（小块被磨没了），
    取数量最多时的那个腐蚀量。这样对输入分辨率不敏感，不必为每种尺寸调参。

    只统计面积达到 min_area 的连通域。少了这个过滤，腐蚀后期会冒出零星碎片把块数
    抬高，从而选出一个腐蚀过头的种子——实测中曾被一个 24 像素的碎片带到 k=5，
    结果一整块（8386 像素）没分到种子。
    """
    best = (0, None, 0)
    for iterations in range(1, SPLIT_ERODE_MAX + 1):
        eroded = cv2.erode(mask, KERNEL, iterations=iterations)
        total, labels, stats, _ = cv2.connectedComponentsWithStats(eroded, 8)
        real = sum(1 for i in range(1, total) if stats[i, cv2.CC_STAT_AREA] >= min_area)
        if real > best[2]:
            best = (iterations, labels, total - 1)
        elif real < best[2]:
            break
    return best


def split_pieces(mask, min_area):
    """
    把物理上挨着的块分开，并保证掩膜里每个像素都被归到某一块。

    相邻的块在掩膜里会连成一个连通域，直接取轮廓会把两块当成一块。做法是：腐蚀出
    「每块一个种子」，再按「到种子的距离」做 Voronoi 划分。腐蚀只用于定位种子，
    块的边界仍由原掩膜决定，因此不会把块削小。
    （对比过用分水岭：两种背景标法都会吃掉约 27% 的掩膜像素。）

    返回 (标记图, 块数)，标记图中 0 = 非块，1..块数 = 各块。
    """
    iterations, seeds_labels, n_labels = _seeds_for_split(mask, min_area)
    region = np.zeros(mask.shape, np.int32)

    if n_labels < 1:
        return region, 0
    if n_labels == 1:
        region[mask > 0] = 1
        return region, 1

    seed_pixels = seeds_labels > 0
    # src 只在种子处为 0，其余全为 1：这样「最近的零像素」就是最近的种子。
    # 注意不能直接用 (seeds == 0) 当 src——掩膜外的像素也是 0，会变成伪种子，
    # 把靠近板边的块抢走，那些像素就被判成非块丢掉了。
    src = np.ones(mask.shape, np.uint8)
    src[seed_pixels] = 0

    # 用一次带标签的距离变换替代「每块各做一次距离变换」：
    # 它给每个像素标出最近零像素的编号，查表即可还原成块编号。
    # 实测这一处就从 20.5ms 降到约 5ms（4 块时）。
    _, nearest = cv2.distanceTransformWithLabels(src, cv2.DIST_L2, 5,
                                                 labelType=cv2.DIST_LABEL_PIXEL)
    lookup = np.zeros(int(nearest.max()) + 1, np.int32)
    lookup[nearest[seed_pixels]] = seeds_labels[seed_pixels]

    region = lookup[nearest]
    region[mask == 0] = 0
    return region, n_labels


def piece_mask(warped_hsv, shape):
    """矫正图上的白块掩膜。整幅矫正图就是板面，不需要再做 ROI。"""
    h, w = shape[:2]
    mask = cv2.inRange(warped_hsv, PIECE_HSV_LOW, PIECE_HSV_HIGH)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, KERNEL)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, KERNEL)

    # 清掉边框：矫正时若四角取得略大，板外的东西会从边上渗进来
    border = np.zeros((h, w), np.uint8)
    cv2.rectangle(border, (BOARD_INSET, BOARD_INSET), (w - 1 - BOARD_INSET, h - 1 - BOARD_INSET), 255, -1)
    return cv2.bitwise_and(mask, border)


def find_pieces(warped_hsv, min_area):
    """在矫正后的板面上找白色块，返回 [{'center','area','rect','polygon','angles'}, ...]。"""
    mask = piece_mask(warped_hsv, warped_hsv.shape)
    labels, count = split_pieces(mask, min_area)

    pieces = []
    for label in range(1, count + 1):
        region = (labels == label).astype(np.uint8) * 255
        found, _ = cv2.findContours(region, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not found:
            continue
        contour = max(found, key=cv2.contourArea)

        area = cv2.contourArea(contour)
        if area < min_area:
            continue

        # 矩形度：面积/最小外接矩形面积。太小的（细长反光条）丢掉
        rect = cv2.minAreaRect(contour)
        (rcx, rcy), (rw, rh), angle = rect
        if rw < 1 or rh < 1 or area / (rw * rh) < 0.45:
            continue

        moments = cv2.moments(contour)
        if moments['m00'] == 0:
            continue

        poly = simplify_polygon(contour)
        pieces.append({
            'center': [round(moments['m10'] / moments['m00'], 1),
                       round(moments['m01'] / moments['m00'], 1)],
            'area': round(area, 1),
            'rect': {
                'center': [round(rcx, 1), round(rcy, 1)],
                'size': [round(rw, 1), round(rh, 1)],
                # 归一到 [0,90)，避开 OpenCV 版本间角度定义的差异
                'angle': round(angle % 90.0, 1),
            },
            'polygon': [[round(float(x), 1), round(float(y), 1)] for x, y in poly],
            'angles': [round(float(a), 1) for a in _interior_angles(poly)],
        })

    # 按面积从大到小，前端叠加层按此顺序编号
    pieces.sort(key=lambda p: -p['area'])
    for index, piece in enumerate(pieces):
        piece['id'] = index
    return pieces


def find_lines(warped_bgr, warped_hsv, occupied, min_area):
    """
    找板面上「颜色和板面主色不一样」的细长结构。

    判据刻意不认颜色：只要偏离板面主色（色相/饱和度/明度任一超阈值）并且细长，
    就算一条线。这样红笔画的线、记号笔的黑线、两块板拼缝那类的深色线都能抓到，
    调参也不必跟着笔的颜色走。

    不在这里猜「哪条是用户要的」——每条都带上实测颜色和位置回传，由上层决定。
    """
    # 板面主色：只用非白块像素算，免得白块把中位色带偏
    free = occupied == 0
    if not free.any():
        return []
    pixels = warped_hsv[free]
    base_h = float(np.median(pixels[:, 0]))
    base_s = float(np.median(pixels[:, 1]))
    base_v = float(np.median(pixels[:, 2]))

    hue = warped_hsv[:, :, 0].astype(np.int16)
    sat = warped_hsv[:, :, 1].astype(np.int16)
    val = warped_hsv[:, :, 2].astype(np.int16)
    # 色相是环形的，差值要取短边（0 和 179 只差 1）
    dh = np.abs(hue - base_h)
    dh = np.minimum(dh, 180 - dh)

    deviant = ((np.abs(val - base_v) > LINE_DEV_V) |
               (dh > LINE_DEV_H) |
               (np.abs(sat - base_s) > LINE_DEV_S))
    mask = (deviant & free).astype(np.uint8) * 255
    h, w = mask.shape
    border = np.zeros(mask.shape, np.uint8)
    cv2.rectangle(border, (BOARD_INSET, BOARD_INSET), (w - 1 - BOARD_INSET, h - 1 - BOARD_INSET), 255, -1)
    mask = cv2.bitwise_and(mask, border)

    min_length = LINE_MIN_LENGTH_RATIO * float(np.hypot(w, h))
    total, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    lines = []
    for i in range(1, total):
        if stats[i, cv2.CC_STAT_AREA] < min_area:
            continue
        ys, xs = np.nonzero(labels == i)
        pts = np.stack([xs, ys], axis=1).astype(np.float32)

        (_, _), (rw, rh), _ = cv2.minAreaRect(pts)
        long_side, short_side = max(rw, rh), min(rw, rh)
        if short_side < 1 or long_side / short_side < LINE_MIN_ELONGATION:
            continue
        if short_side > LINE_MAX_WIDTH or long_side < min_length:
            continue

        # 端点：在主轴方向上的投影取两端
        vx, vy, x0, y0 = cv2.fitLine(pts, cv2.DIST_L2, 0, 0.01, 0.01).ravel()
        axis = np.array([vx, vy])
        proj = (pts - np.array([x0, y0])) @ axis
        p1 = np.array([x0, y0]) + proj.min() * axis
        p2 = np.array([x0, y0]) + proj.max() * axis

        color = np.median(warped_bgr[ys, xs], axis=0)  # BGR
        hsv = np.median(warped_hsv[ys, xs], axis=0)
        r, g, b = float(color[2]), float(color[1]), float(color[0])
        angle = float(np.degrees(np.arctan2(vy, vx)) % 180.0)
        lines.append({
            'p1': [round(float(p1[0]), 1), round(float(p1[1]), 1)],
            'p2': [round(float(p2[0]), 1), round(float(p2[1]), 1)],
            'angle': round(angle, 1),
            'length': round(float(np.linalg.norm(p2 - p1)), 1),
            'width': round(float(short_side), 1),
            'color': {'rgb': [int(round(r)), int(round(g)), int(round(b))],
                      'hsv': [round(float(hsv[0]), 1), round(float(hsv[1]), 1), round(float(hsv[2]), 1)]},
            # 只做一个「像不像红」的粗判，方便上层挑出红笔画的线
            'red': bool(r > 110 and r - max(g, b) > 45 and float(hsv[1]) > 80),
        })

    lines.sort(key=lambda p: -p['length'])
    for index, line in enumerate(lines):
        line['id'] = index
    return lines


def _to_frame(points, h_inv):
    """把矫正空间的点映射回原图坐标。"""
    if not len(points):
        return []
    arr = np.array(points, np.float32).reshape(-1, 1, 2)
    out = cv2.perspectiveTransform(arr, h_inv).reshape(-1, 2)
    return [[round(float(x), 1), round(float(y), 1)] for x, y in out]


def analyze(frame, preview=False):
    """
    入口：BGR 图 → 分析结果 dict。

    流程：找绿板 → 按四角做透视矫正（裁剪 + 摆正）→ 在矫正图上找白块和线条 →
    把所有坐标映射回原图再返回，前端完全不用知道矫正这回事。

    返回结构：
      {
        'frame':  {'width': int, 'height': int},
        'board':  {'found': bool, 'corners': [[x,y]×4] | None,
                   'rect_size': [w, h],          # 矫正后板面的尺寸
                   'homography': [9 float],      # 原图 → 矫正图
                   'bow': {'max_px', 'ratio'}},  # 板边弯曲量，镜头畸变的观测指标
        'pieces': [{'id', 'center', 'area', 'rect', 'polygon', 'angles'}, ...],
        'lines':  [{'id', 'p1', 'p2', 'angle', 'length', 'width', 'color', 'red'}, ...],
        'timing_ms': float,
      }

    preview=True 时额外带上 'board_image'（bytes，**全分辨率**的矫正板面 JPEG）。
    这一项不是 JSON 可序列化的，由 main.py 取走后 base64 编码，别直接 json.dumps。
    """
    started = time.perf_counter()
    h, w = frame.shape[:2]
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)

    corners, _board_mask, contour = find_board(hsv, frame.shape)
    if corners is None:
        return {
            'frame': {'width': w, 'height': h},
            'board': {'found': False, 'corners': None, 'rect_size': None,
                      'homography': None, 'bow': {'max_px': 0.0, 'ratio': 0.0}},
            'pieces': [],
            'lines': [],
            'assembly': None,
            'timing_ms': round((time.perf_counter() - started) * 1000, 2),
        }

    warped, matrix, (rw, rh) = rectify_board(frame, corners)
    warped_hsv = cv2.cvtColor(warped, cv2.COLOR_BGR2HSV)
    h_inv = np.linalg.inv(matrix)

    min_piece = MIN_PIECE_BOARD_RATIO * rw * rh
    pieces = find_pieces(warped_hsv, min_piece)

    # 拼接要用**矫正空间**的多边形（那才是碎片的真实形状），所以赶在坐标映射回原图之前算。
    # 传进去的顺序就是 pieces 的顺序，solve() 会把每块原来排第几个一并带回来。
    assembled = None
    if ASSEMBLY_ENABLE and 2 <= len(pieces) <= ASSEMBLY_MAX_PIECES:
        assembled = assembly.solve([np.array(p['polygon'], np.float64) for p in pieces],
                                   time_budget=ASSEMBLY_TIME_BUDGET)
        if assembled is not None:
            for item in assembled['placements']:
                item['id'] = pieces[item['index']]['id']
                del item['index']

    # 找线之前先把白块占的地方（稍微外扩）挖掉，否则白块的边缘过渡带会被当成线
    occupied = np.zeros((rh, rw), np.uint8)
    if pieces:
        for piece in pieces:
            cv2.fillPoly(occupied, [np.array(piece['polygon'], np.int32)], 255)
        occupied = cv2.dilate(occupied, np.ones((7, 7), np.uint8))
    lines = find_lines(warped, warped_hsv, occupied, LINE_MIN_BOARD_RATIO * rw * rh)

    # 界面的主画面显示的就是**矫正后的板面**，所以矫正空间的坐标要留一份给前端用
    # （前端的叠加层直接按矫正坐标画，不用再自己套一遍单应矩阵）。原图坐标照旧保留，
    # 因为 --source --save 那套调试图是画在原图上的。
    for piece in pieces:
        piece['polygon_warp'] = piece['polygon']
        piece['center'] = _to_frame([piece['center']], h_inv)[0]
        piece['rect']['center'] = _to_frame([piece['rect']['center']], h_inv)[0]
        piece['polygon'] = _to_frame(piece['polygon'], h_inv)
    for line in lines:
        line['p1_warp'], line['p2_warp'] = line['p1'], line['p2']
        line['p1'] = _to_frame([line['p1']], h_inv)[0]
        line['p2'] = _to_frame([line['p2']], h_inv)[0]
        # 长度和线宽也换算到原图尺度：warp 只等比缩放时误差可忽略，
        # 但斜视角下同一段线在矫正前后长度不同，报原图尺度更好对照画面
        line['length'] = round(float(np.linalg.norm(np.array(line['p2']) - np.array(line['p1']))), 1)

    result = {
        'frame': {'width': w, 'height': h},
        'board': {
            'found': True,
            'corners': [[float(x), float(y)] for x, y in corners],
            'rect_size': [rw, rh],
            'homography': [round(float(v), 6) for v in matrix.ravel()],
            'bow': board_bow(corners, contour),
        },
        'pieces': pieces,
        'lines': lines,
        'assembly': assembled,
        'timing_ms': round((time.perf_counter() - started) * 1000, 2),
    }

    if preview:
        # 全分辨率的矫正板面：界面主画面就是拿它当底图显示的。
        # 识别是一次性的，一帧几十 KB 完全无所谓；要是每帧都发就得改协议走二进制了。
        ok, buf = cv2.imencode('.jpg', warped, [int(cv2.IMWRITE_JPEG_QUALITY), 78])
        if ok:
            result['board_image'] = buf.tobytes()
    return result
