"""
视觉侧车进程。

两种用法：

1) stdio 模式（Electron 调用的就是这个）
      python vision/main.py
   从 stdin 读「4 字节小端长度 + JPEG 数据」的帧，处理完把结果以一行 JSON 写回 stdout。
   **stdout 只走协议**，所有日志/警告都去 stderr，否则会污染协议。
   stdin 收到 EOF（Electron 退出）就干净地结束。

2) 独立调试模式（不依赖 Electron、不依赖相机）
      python vision/main.py --source frame.jpg --save out.png   # 单张图 + 输出标注图
      python vision/main.py --source camera:0                   # 实时开窗看效果
      python vision/main.py --selftest                          # 合成图自测，验证算法本身
   调阈值时用这个，比每次重启 Electron 快得多。
"""

import argparse
import base64
import json
import os
import struct
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import assembly  # noqa: E402
import pipeline  # noqa: E402
from pipeline import analyze, rectify_board  # noqa: E402

HEADER_SIZE = 4


def force_utf8_console():
    """
    Windows 上 Python 按系统 locale(中文系统是 cp936) 编码输出，管道/终端里中文会变乱码。
    统一成 UTF-8，和 Electron 侧(也按 UTF-8 解码 stderr)对齐。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass  # 被重定向成非文本流时忽略


# ── stdio 协议 ───────────────────────────────────────────────────────

def set_binary_stdio():
    """
    Windows 上 Python 默认以文本模式打开 stdio，会做 CRLF 转换、把 0x1a 当 EOF，
    二进制帧必然损坏。必须显式切到二进制模式；Linux/macOS 本来就是二进制，无需处理。
    """
    if os.name == 'nt':
        import msvcrt
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)


def read_exact(stream, size):
    """读满 size 字节；流提前结束返回 None。"""
    chunks = []
    remaining = size
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b''.join(chunks)


def send(message):
    payload = json.dumps(message, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    sys.stdout.buffer.write(payload + b'\n')
    sys.stdout.buffer.flush()


def serve():
    set_binary_stdio()
    stdin = sys.stdin.buffer
    while True:
        header = read_exact(stdin, HEADER_SIZE)
        if header is None:
            break  # Electron 关闭了管道
        (size,) = struct.unpack('<I', header)
        payload = read_exact(stdin, size)
        if payload is None:
            break

        frame = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            send({'ok': False, 'error': 'JPEG 解码失败（帧数据不完整或格式不对）'})
            continue

        try:
            # 带预览图：界面的主画面显示的就是矫正后的板面。
            # 里面那项是 bytes，不能直接 json.dumps，得先取出来单独编码。
            result = analyze(frame, preview=True)
            image = result.pop('board_image', None)
            if image is not None:
                result['board_image_b64'] = base64.b64encode(image).decode('ascii')
            result['ok'] = True
            send(result)
        except Exception as error:  # noqa: BLE001 - 单帧失败不该拖垮整个侧车
            send({'ok': False, 'error': f'{type(error).__name__}: {error}'})
    return 0


# ── 独立调试 ─────────────────────────────────────────────────────────

def draw_annotations(frame, result):
    """把分析结果画回**原图**上，方便肉眼核对阈值调得对不对。"""
    canvas = frame.copy()
    board = result['board']
    if board['found']:
        pts = np.array(board['corners'], np.int32)
        cv2.polylines(canvas, [pts], True, (0, 128, 255), 3)
        for index, (x, y) in enumerate(board['corners']):
            cv2.circle(canvas, (int(x), int(y)), 6, (0, 0, 255), -1)
            cv2.putText(canvas, str(index), (int(x) + 8, int(y) - 8),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)

    for piece in result['pieces']:
        poly = np.array(piece['polygon'], np.int32)
        cv2.polylines(canvas, [poly], True, (255, 0, 255), 2)
        cx, cy = piece['center']
        cv2.circle(canvas, (int(cx), int(cy)), 4, (255, 255, 0), -1)
        cv2.putText(canvas, f"#{piece['id']} {len(poly)}v",
                    (int(cx) - 30, int(cy) + 4), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 0), 2)

    draw_lines(canvas, result)

    cv2.putText(canvas, f"pieces={len(result['pieces'])} lines={len(result['lines'])} "
                        f"{result['timing_ms']:.1f}ms",
                (12, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2)
    return canvas


def draw_lines(canvas, result):
    """把「和板面不同色」的线条按**实测颜色**画出来，红笔/黑笔/接缝都适用。"""
    for line in result['lines']:
        p1 = tuple(int(v) for v in line['p1'])
        p2 = tuple(int(v) for v in line['p2'])
        rgb = line['color']['rgb']
        bgr = (int(rgb[2]), int(rgb[1]), int(rgb[0]))
        cv2.line(canvas, p1, p2, bgr, 3)
        # 描一圈白边，免得线本身颜色和背景接近时看不出来
        cv2.circle(canvas, p1, 5, (255, 255, 255), 2)
        cv2.circle(canvas, p2, 5, (255, 255, 255), 2)
        tag = 'RED' if line['red'] else ''
        mid = ((p1[0] + p2[0]) // 2, (p1[1] + p2[1]) // 2)
        cv2.putText(canvas, f"L{line['id']} {line['length']:.0f}px {tag} rgb{rgb}",
                    (mid[0] + 8, mid[1]), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 2)


def draw_rectified(frame, result):
    """把**矫正后的板面**画出来：这是算法真正在上面工作的坐标系，调参看这张最直观。"""
    board = result['board']
    if not board['found']:
        return None
    corners = np.array(board['corners'], np.float32)
    warped, matrix, _size = rectify_board(frame, corners)
    canvas = warped.copy()

    def to_warp(points):
        return cv2.perspectiveTransform(
            np.array(points, np.float32).reshape(-1, 1, 2), matrix).reshape(-1, 2)

    for piece in result['pieces']:
        poly = to_warp(piece['polygon']).astype(np.int32)
        cv2.polylines(canvas, [poly], True, (255, 0, 255), 1)
        # 顶点画成点：锐角被拆成好几个的话，这里会立刻看出一串多余的点
        for x, y in poly:
            cv2.circle(canvas, (int(x), int(y)), 2, (0, 255, 255), -1)
        cx, cy = to_warp([piece['center']])[0]
        cv2.putText(canvas, f"#{piece['id']} {len(poly)}v {min(piece['angles']):.0f}deg",
                    (int(cx) - 40, int(cy) + 4), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                    (255, 255, 0), 1, cv2.LINE_AA)

    for line in result['lines']:
        p1, p2 = to_warp([line['p1']])[0], to_warp([line['p2']])[0]
        rgb = line['color']['rgb']
        cv2.line(canvas, tuple(p1.astype(int)), tuple(p2.astype(int)),
                 (int(rgb[2]), int(rgb[1]), int(rgb[0])), 2)

    cv2.putText(canvas, f"rectified {canvas.shape[1]}x{canvas.shape[0]}  "
                        f"bow {board['bow']['max_px']:.1f}px", (10, 22),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
    return canvas


def draw_assembly(result, size=(560, 420)):
    """把拼好的矩形单独画一张图，方便离线核对拼得对不对。"""
    assembly = result.get('assembly')
    canvas = np.full((size[1], size[0], 3), 26, np.uint8)
    if not assembly:
        cv2.putText(canvas, 'no rectangle found', (18, size[1] // 2),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (150, 150, 150), 2)
        return canvas

    rw, rh = assembly['size']
    pad = 24
    scale = min((size[0] - pad * 2) / max(rw, 1), (size[1] - pad * 2) / max(rh, 1))
    ox = (size[0] - rw * scale) / 2
    oy = (size[1] - rh * scale) / 2
    colors = [(255, 141, 76), (151, 220, 61), (84, 180, 255), (157, 107, 255), (230, 214, 90)]

    for index, item in enumerate(assembly['placements']):
        pts = np.array([[ox + x * scale, oy + y * scale] for x, y in item['polygon']], np.int32)
        color = colors[index % len(colors)]
        cv2.fillPoly(canvas, [pts], tuple(int(c * 0.35) for c in color))
        cv2.polylines(canvas, [pts], True, color, 2)
        c = pts.mean(axis=0).astype(int)
        cv2.putText(canvas, f"#{item['id']}", (int(c[0]) - 12, int(c[1]) + 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)

    cv2.putText(canvas, f"{rw:.0f} x {rh:.0f}  fill {assembly['fill']:.3f}  "
                        f"stretch {assembly['aspect']}",
                (10, size[1] - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (200, 200, 200), 1)
    return canvas


def report(result):
    board = result['board']
    if board['found']:
        corners = ' '.join(f"({x:.0f},{y:.0f})" for x, y in board['corners'])
        print(f"  拼板 : 找到，四角 {corners}", file=sys.stderr)
        rw, rh = board['rect_size']
        print(f"  矫正 : {rw}x{rh}，板边弯曲 {board['bow']['max_px']:.2f}px "
              f"({board['bow']['ratio'] * 100:.2f}% 边长)", file=sys.stderr)
    else:
        print('  拼板 : 未找到（检查绿色阈值 BOARD_HSV_*）', file=sys.stderr)

    print(f"  白块 : {len(result['pieces'])} 块，耗时 {result['timing_ms']} ms", file=sys.stderr)
    for piece in result['pieces']:
        cx, cy = piece['center']
        w, h = piece['rect']['size']
        angles = ' '.join(f"{a:.0f}" for a in piece['angles'])
        print(f"    #{piece['id']} 中心({cx:.0f},{cy:.0f}) "
              f"{w:.0f}x{h:.0f} 角度{piece['rect']['angle']:.1f}° 面积{piece['area']:.0f} "
              f"顶点{len(piece['polygon'])} 内角[{angles}]",
              file=sys.stderr)

    print(f"  线条 : {len(result['lines'])} 条", file=sys.stderr)
    for line in result['lines']:
        rgb = line['color']['rgb']
        print(f"    L{line['id']} {line['length']:.0f}px 宽{line['width']:.1f} "
              f"角度{line['angle']:.1f}° rgb{tuple(rgb)}"
              f"{'  ← 判定为红色' if line['red'] else ''}", file=sys.stderr)

    detail = result.get('piece_mode_detail') or {}
    print(f"  判型 : {'花纹（碎片上有印刷，按「不是板面颜色」抠）' if result.get('piece_mode') == 'pattern' else '纯色（按碎片颜色抠）'}"
          f"　碎片内部非主色像素占 {detail.get('ink_ratio', 0):.3f}"
          f"（门槛 {pipeline.PIECE_MODE_INK_RATIO}，各块 {detail.get('per_piece')}）",
          file=sys.stderr)

    assembly = result.get('assembly')
    if assembly:
        how = '边匹配（真凑成矩形）' if assembly.get('strict') else '打分搜索兜底（未必是矩形）'
        print(f"  拼接 : 拼成 {assembly['size'][0]:.0f}×{assembly['size'][1]:.0f} 矩形"
              f"（{how}，外轮廓 {assembly['outer_vertices']} 个顶点，填充率 {assembly['fill']:.4f}，"
              f"碎块内缩 {assembly['shrink_px']}px，总分 {assembly['score']}）", file=sys.stderr)
        for item in assembly['placements']:
            print(f"    #{item['id']} 落在 "
                  f"{[[round(x), round(y)] for x, y in item['polygon']]}", file=sys.stderr)
    else:
        print("  拼接 : 这次没拼出矩形（不猜、不糊弄，宁可报没有）", file=sys.stderr)


def run_source(source, save_path):
    if source.startswith('camera:'):
        index = int(source.split(':', 1)[1])
        cap = cv2.VideoCapture(index)
        if not cap.isOpened():
            print(f'打不开摄像头 {index}', file=sys.stderr)
            return 1
        print('实时预览中，按 q 或 Esc 退出', file=sys.stderr)
        print('左窗 = 原图叠加，右窗 = 矫正后的板面', file=sys.stderr)
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            result = analyze(frame)
            cv2.imshow('puzzle-cam vision (q 退出)', draw_annotations(frame, result))
            rect = draw_rectified(frame, result)
            if rect is not None:
                cv2.imshow('puzzle-cam 矫正板面', rect)
            if cv2.waitKey(1) & 0xFF in (ord('q'), 27):
                break
        cap.release()
        cv2.destroyAllWindows()
        return 0

    frame = cv2.imread(source)
    if frame is None:
        print(f'读不出图片：{source}', file=sys.stderr)
        return 1
    result = analyze(frame)
    report(result)
    if save_path:
        cv2.imwrite(save_path, draw_annotations(frame, result))
        print(f'  标注图已写到 {save_path}', file=sys.stderr)
        rect = draw_rectified(frame, result)
        if rect is not None:
            rect_path = os.path.splitext(save_path)[0] + '_rect.png'
            cv2.imwrite(rect_path, rect)
            print(f'  矫正板面已写到 {rect_path}', file=sys.stderr)
        asm_path = os.path.splitext(save_path)[0] + '_assembly.png'
        cv2.imwrite(asm_path, draw_assembly(result))
        print(f'  拼接结果已写到 {asm_path}', file=sys.stderr)
    print(json.dumps(result, ensure_ascii=False)[:400], file=sys.stderr)
    return 0


def rounded_wedge(tip, a, b, radius, steps=24):
    """
    把 tip 处的尖角换成半径 radius 的圆弧。

    真实的尖角在掩膜里就是被这样磨圆的（抗锯齿 + 形态学），所以自测用这个形状来复现
    「锐角被识别成一串钝角」的场景。
    """
    tip = np.array(tip, float)
    a, b = np.array(a, float), np.array(b, float)
    u = (a - tip) / np.linalg.norm(a - tip)
    v = (b - tip) / np.linalg.norm(b - tip)
    half = np.arccos(np.clip(np.dot(u, v), -1, 1)) / 2
    tangent_dist = radius / np.tan(half)
    center = tip + (u + v) / np.linalg.norm(u + v) * (radius / np.sin(half))
    t1, t2 = tip + u * tangent_dist, tip + v * tangent_dist
    a1 = np.arctan2(*(t1 - center)[::-1])
    a2 = np.arctan2(*(t2 - center)[::-1])
    sweep = (a2 - a1 + np.pi) % (2 * np.pi) - np.pi
    arc = [center + radius * np.array([np.cos(a1 + sweep * k / steps),
                                       np.sin(a1 + sweep * k / steps)])
           for k in range(steps + 1)]
    return np.array([a] + [t1] + arc + [t2, b], np.int32)


def _polygon_angles(polygon):
    """多边形内角（升序）。刚体变换和平移内缩都不改变内角，所以拿它验「编号有没有错位」最稳。"""
    pts = np.asarray(polygon, float)
    n = len(pts)
    out = []
    for i in range(n):
        v1 = pts[(i - 1) % n] - pts[i]
        v2 = pts[(i + 1) % n] - pts[i]
        cos = np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2))
        out.append(float(np.degrees(np.arccos(np.clip(cos, -1.0, 1.0)))))
    return sorted(out)


def _selftest_assembly():
    """
    拼接求解器的合成用例：自己切一个 240×150 的矩形成 4 块，看能不能还原。

    这是唯一能确定「求解器本身没坏」的检查——真实碎片的几何受测量误差影响，
    拼不出来未必是求解器的错，所以两者要分开验。
    """
    rectangle = 240.0 * 150.0
    pieces = [
        np.array([[0, 0], [0, 150], [90, 150]], np.float64),
        np.array([[90, 150], [240, 150], [240, 60]], np.float64),
        np.array([[0, 0], [240, 0], [240, 60]], np.float64),
        np.array([[0, 0], [240, 60], [90, 150]], np.float64),
    ]
    problems = []
    got = assembly.solve(pieces, time_budget=30.0)
    if got is None:
        problems.append('合成剖分没拼出来（求解器坏了）')
        return problems
    # 解出来的矩形面积应当等于各块面积之和
    solved = got['size'][0] * got['size'][1]
    if abs(solved - rectangle) > 0.03 * rectangle:
        problems.append(f'合成剖分解出的矩形面积 {solved:.0f}，应接近 {rectangle:.0f}')
    if got['fill'] < 0.98:
        problems.append(f"合成剖分的栅格填充率只有 {got['fill']:.4f}，不是真矩形")
    if sorted(p['index'] for p in got['placements']) != [0, 1, 2, 3]:
        problems.append('拼出来的碎块编号不是 0..3，块对应错了')
    print(f"  拼接 : 合成剖分还原成 {got['size'][0]:.0f}×{got['size'][1]:.0f}"
          f"（应 {240}×{150}，填充率 {got['fill']:.4f}）", file=sys.stderr)

    # 拼不成矩形的四块**必须**明确返回 None。
    #
    # 这条是「拼出来的矩形不对」那件事的回归测试：原来那套打分搜索不管三七二十一
    # 总能挑出一个「最不难看」的摆法交出来，于是摆得乱七八糟的结果照样被画到屏幕上、
    # 机器照样照着摆。现在要求「要么是真矩形，要么说没有」。
    junk = [
        np.array([[0, 0], [200, 0], [140, 40]], np.float64),                     # 细长三角
        np.array([[0, 120], [60, 120], [30, 170]], np.float64),                  # 小三角
        np.array([[200, 200], [330, 210], [340, 300], [210, 290]], np.float64),  # 方块
        np.array([[500, 60], [560, 20], [600, 90], [540, 130]], np.float64),     # 菱形
    ]
    # 这里专测**严格**那条路（fallback=False）：兜底那套一定能交出个东西来，
    # 拿它去测「拼不成矩形就该说拼不出来」等于没测
    junk_got = assembly.solve([p.copy() for p in junk], time_budget=10.0, fallback=False)
    if junk_got is None:
        print('  拼接 : 拼不成矩形的四块 → 明确报「拼不出来」（对）', file=sys.stderr)
    elif junk_got['fill'] < 0.95:
        problems.append(f"拼不成矩形的四块给出了结果 {junk_got['size']}"
                        f"（填充率只有 {junk_got['fill']:.4f}）—— 这就是「拼错了还照样摆」")
    else:
        print(f"  拼接 : 这四块居然拼出了 {junk_got['fill']:.4f} 的矩形，值得看一眼",
              file=sys.stderr)
    return problems


def _selftest_pattern():
    """
    花纹（扑克牌）那一套：合成一张「绿板 + 四块印了花的卡片」，验证

      1. 判型认得出这是花纹；
      2. 每块都还是**四条边、四个角接近 90°**——印刷没有在轮廓上啃出缺口；
      3. 多边形面积贴合卡片本身的面积（丢角就会明显偏小）。

    这条是「纯色那套抠花纹块会崩」的回归：同一张图硬走纯色掩膜，
    实测会冒出 16°/46° 的假角、顶点数从 4 涨到 7（在下面顺带打出来做对照）。

    花色必须**被卡片的边裁掉**（画在 ROI 里，超出部分自然没了）——实物就是这样，
    刀切过之后墨只留在块的里面、紧贴着切边。把花色画到卡片外面去是不对的：
    那不是「印在切边上的花」，那是往板子上画了个点。
    """
    problems = []
    frame = np.full((720, 1280, 3), 210, np.uint8)
    cv2.fillConvexPoly(frame, np.array([[200, 120], [1050, 90], [1090, 640], [170, 660]], np.int32),
                       (60, 200, 60))
    boxes = [(300, 200, 500, 330), (620, 180, 800, 320),
             (330, 400, 500, 525), (630, 415, 810, 545)]
    black, red = (20, 20, 20), (30, 30, 210)

    def card(box, marks):
        x0, y0, x1, y1 = box
        roi = frame[y0:y1, x0:x1]
        roi[:] = (248, 248, 248)
        layer = np.zeros_like(roi)
        for kind, args in marks:
            if kind == 'dot':
                cv2.circle(layer, args[0], args[1], args[2], -1)
            else:
                cv2.rectangle(layer, args[0], args[1], args[2], -1)
        hit = cv2.cvtColor(layer, cv2.COLOR_BGR2GRAY) > 0
        roi[hit] = layer[hit]

    # 两块的花色正好压在切边上（局部坐标 x=0 / y=0 那两个点会被裁掉一半）
    card(boxes[0], [('dot', ((0, 65), 30, black)), ('dot', ((150, 100), 18, red))])
    card(boxes[1], [('dot', ((60, 70), 26, red)), ('rect', ((140, 30), (180, 60), black))])
    card(boxes[2], [('dot', ((70, 60), 24, red)), ('rect', ((20, 92), (140, 108), black))])
    card(boxes[3], [('dot', ((70, 0), 30, black)), ('rect', ((90, 15), (160, 50), red))])

    saved_budget = pipeline.ASSEMBLY_TIME_BUDGET
    pipeline.ASSEMBLY_TIME_BUDGET = 2.0
    try:
        result = analyze(frame)
    finally:
        pipeline.ASSEMBLY_TIME_BUDGET = saved_budget

    detail = result.get('piece_mode_detail') or {}
    print(f"  花纹 : 判型 {result.get('piece_mode')}（印花占比 {detail.get('ink_ratio')}）",
          file=sys.stderr)
    if result.get('piece_mode') != 'pattern':
        problems.append(f"卡片图没被判成花纹，判成了 {result.get('piece_mode')}"
                        f"（印花占比 {detail.get('ink_ratio')}）")
    if len(result['pieces']) != len(boxes):
        problems.append(f'卡片数量不符：期望 {len(boxes)}，实际 {len(result["pieces"])}')
        return problems

    # 每块配回它自己那张卡片：**按重心就近配，不能按 id**——pieces 是按面积从大到小
    # 排过序的，id 和 boxes 的顺序对不上（第一版就是这里配错了，报出 79% 的假失败）
    bad = 0
    for piece in result['pieces']:
        # 面积一律在**原图坐标**里比：卡片是画在原图上的。
        # （piece['area'] 是矫正空间的面积，拿它比原图的框是两套单位，第一版就这么错的）
        poly = np.array(piece['polygon'], np.float32).reshape(-1, 1, 2)
        n = len(piece['polygon'])
        angles = [round(float(a)) for a in piece['angles']]
        # 卡片是方的：四条边、四个角都该接近 90°。印刷啃出缺口就会多出假角
        if n != 4:
            problems.append(f"#{piece['id']} 卡片抠出了 {n} 条边（该是 4）：内角 {angles}")
            bad += 1
            continue
        worst = max(abs(a - 90.0) for a in angles)
        if worst > 12.0:
            problems.append(f"#{piece['id']} 有角不接近 90°：{angles}")
            bad += 1
        cx, cy = piece['center']
        box = min(boxes, key=lambda b: (0.5 * (b[0] + b[2]) - cx) ** 2
                  + (0.5 * (b[1] + b[3]) - cy) ** 2)
        want = abs((box[2] - box[0]) * (box[3] - box[1]))
        got = abs(cv2.contourArea(poly))
        if got < 0.95 * want:
            problems.append(f"#{piece['id']} 多边形面积只有卡片的 {got / want * 100:.0f}%"
                            f"（印刷把轮廓啃缺了）")
            bad += 1
    print(f"  花纹 : {len(result['pieces'])} 块卡片，"
          f"{'边数/角度/面积都对得上' if not bad else f'{bad} 项不对'}", file=sys.stderr)
    return problems


def run_selftest():
    """
    合成一张「绿板 + 白块 + 一条红线」的图，验证算法本身没被改坏。

    特意构造了两个容易出问题的形状：
      - 一个**被磨圆的锐角**：检查它仍然只报 1 个顶点、角度还接近真值，
        而不是被拆成好几个钝角（反馈里的那个毛病）
      - 一个**边长很短、角被磨圆的小三角形**：简化器的安全阀一度在这里把尖角还原
        整个拒掉，多边形只剩区域面积的 85%。面积校验就是冲它来的。
        注意必须做得**小**：削掉的角是固定的像素量，而 epsilon 跟周长成正比，
        块越小削掉的比例越大，所以这个 bug 只在小块上现形。

    这张图也是**纯色判型**的反向用例：必须判成 solid，而且各块多边形要和改动前一致。
    """
    frame = np.full((720, 1280, 3), 210, np.uint8)          # 浅色桌面
    cv2.fillConvexPoly(frame, np.array([[200, 120], [1050, 90], [1090, 640], [170, 660]], np.int32),
                       (60, 200, 60))                        # 绿板，故意做成斜的

    tip, base_a, base_b = (560, 290), (330, 170), (330, 410)
    true_tip_angle = float(np.degrees(np.arccos(np.clip(np.dot(
        (np.array(base_a) - tip) / np.linalg.norm(np.array(base_a) - tip),
        (np.array(base_b) - tip) / np.linalg.norm(np.array(base_b) - tip)), -1, 1))))
    small_tip, small_a, small_b = (940, 220), (860, 175), (860, 265)

    shapes = [
        rounded_wedge(tip, base_a, base_b, 8),                              # 磨圆的大锐角
        rounded_wedge(small_tip, small_a, small_b, 5),                      # 磨圆的小锐角（回归用）
        np.array([[300, 470], [470, 480], [480, 600], [310, 590]], np.int32),
        np.array([[800, 430], [900, 380], [920, 545]], np.int32),
    ]
    for shape in shapes:
        cv2.fillPoly(frame, [shape], (250, 250, 250))

    # 红线：竖着画在板子中间，左右各留出足够的空隙，避开所有白块
    line_x = 700
    cv2.line(frame, (line_x, 180), (line_x, 620), (0, 0, 255), 5)

    problems = _selftest_assembly() + _selftest_pattern()

    # 自测不必等满拼接预算，不然每跑一次都要十几秒
    saved_budget = pipeline.ASSEMBLY_TIME_BUDGET
    pipeline.ASSEMBLY_TIME_BUDGET = 2.0
    try:
        result = analyze(frame)
    finally:
        pipeline.ASSEMBLY_TIME_BUDGET = saved_budget

    # 纯色判型的反向用例：这张图上全是实心白块，必须判成 solid。
    # 判成花纹不至于崩（花纹那套抠实心块一样能抠对），但那是白白绕远路，
    # 而且说明判据已经不认得了——真要出现就得看一眼。
    if result.get('piece_mode') != 'solid':
        problems.append(f"这张纯色图被判成了 {result.get('piece_mode')}"
                        f"（印花占比 {result.get('piece_mode_detail', {}).get('ink_ratio')}）")

    if not result['board']['found']:
        problems.append('没找到绿板')
    else:
        if len(result['board']['corners']) != 4:
            problems.append(f"拼板角点数不是 4：{len(result['board']['corners'])}")
        rw, rh = result['board']['rect_size']
        if not (160 <= rw <= 800 and 120 <= rh <= 600):
            problems.append(f'矫正尺寸超出 WARP_MIN/WARP_MAX：{rw}x{rh}')

    if len(result['pieces']) != len(shapes):
        problems.append(f'白块数量不符：期望 {len(shapes)}，实际 {len(result["pieces"])}')

    # 面积校验：多边形应当贴合它所属的区域。掩膜的角是圆的，所以简化后略大是对的，
    # 但**明显偏小就说明丢角了**。这条是整个自测里最灵敏的一项。
    if result['board']['found']:
        homography = np.array(result['board']['homography'], np.float32).reshape(3, 3)
        for piece in result['pieces']:
            poly = np.array(piece['polygon'], np.float32).reshape(-1, 1, 2)
            warped_poly = cv2.perspectiveTransform(poly, homography).reshape(-1, 2)
            if piece['area'] <= 0:
                continue
            ratio = abs(cv2.contourArea(warped_poly)) / piece['area']
            if not 0.90 <= ratio <= 1.15:
                problems.append(f"#{piece['id']} 多边形面积是区域的 {ratio * 100:.0f}%"
                                '（<90% 说明丢角，>115% 说明顶点飞了）')

    # 锐角：应当只被报成一个顶点，角度接近真值。
    # 认「哪个顶点压在尖角上」来选那块，别按中心距离——别的块的中心可能离尖角更远。
    if result['pieces']:
        tip_piece = min(result['pieces'], key=lambda p: min(
            np.linalg.norm(np.array(v) - np.array(tip)) for v in p['polygon']))
        sharpest = min(tip_piece['angles'])
        if len(tip_piece['polygon']) != 3:
            problems.append(f"磨圆锐角那块报出 {len(tip_piece['polygon'])} 个顶点，应为 3")
        if abs(sharpest - true_tip_angle) > 12:
            problems.append(f'锐角角度 {sharpest:.1f}°，真值 {true_tip_angle:.1f}°，偏差过大')

    # 拼接：要么给出一个栅格化验证过的真矩形，要么明确是 None，不许给「看着像」的东西
    # 拼接：这张合成图里的白块是随手画的形状，本来就拼不成矩形，所以这里**不能**断言
    # 「必须是矩形」——那会变成一个永远失败的假测试。求解器本身的正确性由
    # _selftest_assembly() 用已知答案的剖分来验。这里只验结果的结构自洽。
    assembly_result = result.get('assembly')
    if assembly_result is not None:
        if sorted(p['id'] for p in assembly_result['placements']) != \
                sorted(p['id'] for p in result['pieces']):
            problems.append('拼接结果里的碎块编号和识别到的对不上')

        # 每块拼好的形状，必须是**它自己那块**刚体变换过去的。刚体变换和平移内缩都不改变
        # 内角，所以内角对不上就是编号错位了——曾经踩过：placed 按插入顺序、ids 却排了序，
        # 结果左边拼好的矩形里，某一块的形状和它标的编号对不上。
        for item in assembly_result['placements']:
            piece = next((p for p in result['pieces'] if p['id'] == item['id']), None)
            if piece is None or not piece.get('polygon_warp'):
                continue
            want = _polygon_angles(piece['polygon_warp'])
            got = _polygon_angles(item['polygon'])
            if len(want) != len(got) or any(abs(a - b) > 1.5 for a, b in zip(want, got)):
                problems.append(
                    f"#{item['id']} 拼好后的形状和它自己那块对不上（编号错位）："
                    f"{[round(a, 1) for a in want]} vs {[round(a, 1) for a in got]}")

            # rot_deg 必须是真的能把这块转过去的角：按它转完，应当落在目标多边形上。
            #
            # 这条防的是「角度算错了但看着像那么回事」——曾经在前端拿「碎片第 0 条边」和
            # 「目标第 0 条边」的方向相减去算角度，而两边绕向相反（拼接前统一转过），
            # 「第 0 条边」根本不是同一条边，4 块里错了 3 块，误差 18~59 像素。
            # rot_deg 现在由求解器按**实际施加的旋转矩阵**给出，这条把它钉住。
            src = np.asarray(piece['polygon_warp'], float)
            target = np.asarray(item['polygon'], float)
            if len(src) == len(target) and 'rot_deg' in item:
                # 要和**内缩之后**的那块比：placement 是内缩后那块刚体变换过去的，
                # 拿没内缩的原始形状比会差上几个像素，内缩量大时能差十几像素——那是内缩
                # 造成的，不是角度错。比内缩后的形状，关系是精确的，阈值就能卡得很紧。
                # _ccw 不能省：_shrink_poly 是按逆时针往内缩的，喂它一个顺时针多边形
                # 会变成往外扩（solve() 里也是先 _ccw 再 _shrink_poly）
                shrunk = assembly._shrink_poly(assembly._ccw(src),
                                               assembly_result.get('shrink_px', 0.0))
                rad = np.radians(item['rot_deg'])
                rot = np.array([[np.cos(rad), -np.sin(rad)], [np.sin(rad), np.cos(rad)]])
                moved = (shrunk - shrunk.mean(axis=0)) @ rot.T + target.mean(axis=0)
                # 比「到最近目标点」的距离：两边绕向可能相反，逐点按编号比没意义
                gap = np.linalg.norm(moved[:, None, :] - target[None, :, :], axis=2).min(axis=1)
                if gap.max() > 2.0:
                    problems.append(
                        f"#{item['id']} 按 rot_deg={item['rot_deg']}° 转过去落不到目标上"
                        f"（差 {gap.max():.1f}px），角度算错了")
        if not 0.0 <= assembly_result['fill'] <= 1.0:
            problems.append(f"拼接填充率 {assembly_result['fill']} 不在 0~1 之间，算错了")
        rw_, rh_ = assembly_result['size']
        if rw_ <= 0 or rh_ <= 0:
            problems.append('拼接结果的尺寸不是正数')

    # 红线：应当被抓到，并且按实测颜色判成红色
    red_lines = [line for line in result['lines'] if line['red']]
    if not result['lines']:
        problems.append('板面上的红线没被检测到')
    elif not red_lines:
        colors = [line['color']['rgb'] for line in result['lines']]
        problems.append(f'检测到 {len(result["lines"])} 条线但都不像红色，实测颜色 {colors}')
    else:
        best = max(red_lines, key=lambda line: line['length'])
        mid_x = (best['p1'][0] + best['p2'][0]) / 2
        mid_y = (best['p1'][1] + best['p2'][1]) / 2
        if abs(mid_x - line_x) > 30 or abs(mid_y - 400) > 60:
            problems.append(f'红线位置不对：中点 ({mid_x:.0f},{mid_y:.0f})，期望接近 ({line_x},400)')

    report(result)
    for message in problems:
        print(f'  FAIL {message}', file=sys.stderr)
    print('\nSELFTEST PASS' if not problems else f'\nSELFTEST FAIL ({len(problems)})', file=sys.stderr)
    return 0 if not problems else 1


def main():
    parser = argparse.ArgumentParser(description='拼板视觉侧车')
    parser.add_argument('--source', help='图片路径，或 camera:0 用摄像头实时调试')
    parser.add_argument('--save', help='把标注结果图写到这个路径')
    parser.add_argument('--selftest', action='store_true', help='跑合成图自测')
    args = parser.parse_args()

    force_utf8_console()

    if args.selftest:
        return run_selftest()
    if args.source:
        return run_source(args.source, args.save)
    return serve()


if __name__ == '__main__':
    sys.exit(main())
