import sys, os, platform, ctypes, json, math
from typing import Optional, List, Dict

from PySide6.QtCore import Qt, QPoint, QRect, QSize, Signal, QLockFile, QTimer, QEvent
from PySide6.QtGui import (
    QPixmap, QGuiApplication, QPainter, QImageReader, QPixmapCache,
    QCursor, QColor, QPainterPath, QPolygon
)
from PySide6.QtWidgets import (
    QApplication, QWidget, QToolButton, QMenu,
    QMainWindow, QFileDialog, QHBoxLayout, QSystemTrayIcon, QStyle
)

# ================== Windows 전용 등록/메타 ==================
try:
    import winreg
except Exception:
    winreg = None

if platform.system() == "Windows":
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MyCompany.StickerBoard.1")
    except Exception:
        pass

APP_RUN_VALUE = "StickerBoard"
BTN_MARGIN = 6

# 버튼 크기 상수(고정)
CLOSE_BTN_W = 18
CLOSE_BTN_H = 18
ROT_BTN_W = 18
ROT_BTN_H = 18

ROT_EXTRA_GAP = 12
MIN_SIDE = 64  # 짧은 변 최소 보장 (비율 유지)

IS_EXITING = False


# ================== 저장 매니저 ==================
class SaveManager:
    def __init__(self):
        if platform.system() == "Windows":
            base = r"C:\StickerBoard"
        else:
            base = os.path.join(os.path.expanduser("~"), "StickerBoard")
        os.makedirs(base, exist_ok=True)
        self.base = base
        self.path = os.path.join(base, "Save.dat")

    def load(self) -> Optional[Dict]:
        try:
            if not os.path.exists(self.path):
                return None
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def save(self, data: Dict) -> None:
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                f.flush(); os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except Exception:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass


def WT(name):
    return getattr(Qt.WindowType, name) if hasattr(Qt, "WindowType") else Qt.__dict__[name]

def wf(*flags):
    base = Qt.WindowType(0) if hasattr(Qt, "WindowType") else Qt.WindowFlags(0)
    for f in flags:
        base |= f
    return base


# ================== Pixmap 유틸 (저메모리) ==================
try:
    QImageReader.setAllocationLimit(256 * 1024 * 1024)  # bytes
except Exception:
    pass

def _cache_find(key: str) -> Optional[QPixmap]:
    try:
        pm = QPixmapCache.find(key)
        if isinstance(pm, QPixmap) and not pm.isNull():
            return pm
    except TypeError:
        pm = QPixmap()
        if QPixmapCache.find(key, pm):
            return pm
    return None

def _cache_insert(key: str, pm: QPixmap) -> None:
    QPixmapCache.insert(key, pm)

def _cache_key(path: str) -> str:
    try:
        mt = os.path.getmtime(path)
    except OSError:
        mt = 0
    return f"{path}|{mt}"

def load_pixmap_fixed(path: str, target: QSize, device_ratio: float) -> QPixmap:
    key = _cache_key(path) + f"|{target.width()}x{target.height()}@{device_ratio:.2f}"
    pm = _cache_find(key)
    if pm:
        return pm

    reader = QImageReader(path)
    reader.setAutoTransform(False)
    if reader.size().isValid():
        reader.setScaledSize(QSize(
            max(1, int(target.width() * device_ratio)),
            max(1, int(target.height() * device_ratio))
        ))
    img = reader.read()
    if img.isNull():
        pm = QPixmap(target); pm.fill(Qt.transparent)
    else:
        pm = QPixmap.fromImage(img)
        pm.setDevicePixelRatio(device_ratio)

    _cache_insert(key, pm)
    return pm


# ================== 시작프로그램 유틸 ==================
def _win_is_startup_enabled(name: str) -> bool:
    if platform.system() != "Windows" or winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Run",
                            0, winreg.KEY_READ) as key:
            try:
                _ = winreg.QueryValueEx(key, name)
                return True
            except FileNotFoundError:
                return False
    except OSError:
        return False

def _win_get_pythonw_path() -> Optional[str]:
    exe = sys.executable
    if exe and exe.lower().endswith("python.exe"):
        candidate = exe[:-9] + "pythonw.exe"
        if os.path.exists(candidate):
            return candidate
    return exe

def _win_get_startup_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    py = _win_get_pythonw_path() or sys.executable
    script = os.path.abspath(sys.argv[0])
    return f'"{py}" "{script}"'

def _win_set_startup(name: str, enable: bool) -> bool:
    if platform.system() != "Windows" or winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Run",
                            0, winreg.KEY_SET_VALUE | winreg.KEY_READ) as key:
            if enable:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, _win_get_startup_command())
            else:
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False


# ================== StickerWindow ==================
class StickerWindow(QWidget):
    stateChanged = Signal()

    __slots__ = (
        "dragging", "resizing", "rotating",
        "resize_margin", "rotation_start_x", "rotate_scale",
        "start_angle",
        "drag_start", "win_start", "menu", "image_path", "aspect_ratio", "device_ratio",
        "btn_close", "btn_rotate", "locked", "rotation_angle",
        "base_w", "base_h", "pm_base",
        "_resizing_pending_size",
        "_resize_tri", "_resize_tri_pts",
        "_start_base_w", "_start_base_h",
        "_mask_pending",
        "_saved_center", "_pos_fix_applied",
        "_resize_anchor_pt",
        "_scale_ema",
        "_last_applied_geom",
        "_show_resize_handle",              # ★ 리사이즈 핸들 가시성
        "_act_lock", "_act_top", "_act_bot", "_act_reset", "_act_close"
    )

    def __init__(self, image_path: str, start_pos: Optional[QPoint] = None, *, initial_topmost: bool = True):
        super().__init__(parent=None)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)
        self.setMouseTracking(True)
        self.setContentsMargins(0, 0, 0, 0)
        self.setWindowFlags(wf(WT("FramelessWindowHint"), WT("Tool"), WT("NoDropShadowWindowHint")))

        self.image_path = image_path
        self.setWindowTitle(os.path.basename(image_path))

        # 상태
        self.dragging = False
        self.resizing = False
        self.rotating = False
        self.resize_margin = 18
        self.rotate_scale = 0.5
        self.locked = False
        self.rotation_angle = 0.0
        self._mask_pending = False

        self._resizing_pending_size = None
        self._resize_tri = None
        self._resize_tri_pts = None
        self._start_base_w = None
        self._start_base_h = None

        self._saved_center: Optional[QPoint] = None
        self._pos_fix_applied = False
        self._resize_anchor_pt: Optional[QPoint] = None
        self._scale_ema: float = 1.0
        self._last_applied_geom: Optional[QRect] = None
        self._show_resize_handle = False  # ★

        # 화면 스케일
        screen = self.screen() or QGuiApplication.primaryScreen()
        self.device_ratio = (screen.devicePixelRatio() if screen else 1.0) or 1.0

        # 초기 크기(비율 유지 + 짧은 변 최소 보장)
        reader = QImageReader(image_path)
        natural = reader.size() if reader.size().isValid() else QSize(300, 300)
        self.aspect_ratio = (natural.width() / natural.height()) if natural.height() > 0 else 1.0

        scr = self.screen() or QGuiApplication.primaryScreen()
        scr_size = scr.availableGeometry().size() if scr else QSize(1920, 1080)
        max_w = int(min(scr_size.width() * 0.35, 720))
        max_h = int(min(scr_size.height() * 0.35, 720))
        fit_scale = min(max_w / max(1, natural.width()), max_h / max(1, natural.height()), 1.0)
        base_scale = 1.0 if natural.width() <= 320 or natural.height() <= 320 else 0.6
        s = min(fit_scale, base_scale)

        w0 = max(1, int(natural.width() * s))
        h0 = max(1, int(natural.height() * s))
        short = min(w0, h0)
        if short < MIN_SIDE:
            scale_fix = MIN_SIDE / float(short)
            w0 = int(round(w0 * scale_fix))
            h0 = int(round(h0 * scale_fix))
        self.base_w = w0
        self.base_h = h0

        self.pm_base = load_pixmap_fixed(self.image_path, QSize(self.base_w, self.base_h), self.device_ratio)

        side = self._max_square_side(self.base_w, self.base_h)
        self.resize(side, side)

        # 버튼
        self.btn_close = QToolButton(self)
        self.btn_close.setText("✕")
        self.btn_close.setToolTip("닫기")
        self.btn_close.setStyleSheet(
            "QToolButton { background: rgba(0,0,0,160); color: white; border: none;"
            f"  border-radius: {CLOSE_BTN_W//2}px; width: {CLOSE_BTN_W}px; height: {CLOSE_BTN_H}px; font-weight: bold; }}"
            "QToolButton:hover { background: rgba(220,40,40,220); }"
        )
        self.btn_close.setFixedSize(CLOSE_BTN_W, CLOSE_BTN_H)
        self.btn_close.clicked.connect(self.close)

        self.btn_rotate = QToolButton(self)
        self.btn_rotate.setText("↻")
        self.btn_rotate.setToolTip("드래그해서 회전")
        self.btn_rotate.setStyleSheet(
            "QToolButton { background: rgba(0,0,0,140); color: white; border: none;"
            f"  border-radius: {ROT_BTN_W//2}px; width: {ROT_BTN_W}px; height: {ROT_BTN_H}px; font-weight: bold; }}"
            "QToolButton:hover { background: rgba(0,0,0,200); }"
        )
        self.btn_rotate.setFixedSize(ROT_BTN_W, ROT_BTN_H)
        self.btn_rotate.pressed.connect(self._begin_rotate_by_button)

        # 버튼 가시성: 이미지 위에서만 보이기
        self.btn_close.setVisible(False)
        self.btn_rotate.setVisible(False)
        self.btn_close.setMouseTracking(True)
        self.btn_rotate.setMouseTracking(True)
        self.btn_close.installEventFilter(self)
        self.btn_rotate.installEventFilter(self)

        # 컨텍스트 메뉴
        self.menu = QMenu(self)
        self._act_lock = self.menu.addAction("위치 고정"); self._act_lock.setCheckable(True)
        self._act_top = self.menu.addAction("최상단에 띄우기")
        self._act_bot = self.menu.addAction("바탕화면에 띄우기")
        self.menu.addSeparator()
        self._act_reset = self.menu.addAction("회전 초기화")
        self.menu.addSeparator()
        self._act_close = self.menu.addAction("닫기")
        self.menu.triggered.connect(self._on_menu)

        if start_pos:
            self.move(start_pos)
        if initial_topmost:
            self._apply_topmost(True)

        self._place_overlay_controls()
        self._apply_rotated_rect_mask()
        self._update_overlay_visibility()

    # ======= 수학 유틸 =======
    @staticmethod
    def _max_square_side(w: int, h: int) -> int:
        return int(math.ceil(math.sqrt(w*w + h*h))) + 2

    def _map_image_center_to_widget(self, vx: float, vy: float) -> QPoint:
        a = math.radians(self.rotation_angle or 0.0)
        ca, sa = math.cos(a), math.sin(a)
        rx = vx * ca - vy * sa
        ry = vx * sa + vy * ca
        cx, cy = self.rect().center().x(), self.rect().center().y()
        return QPoint(int(round(cx + rx)), int(round(cy + ry)))

    def _is_pos_in_image(self, pos: QPoint) -> bool:
        w, h = self._current_image_w_h()
        if w <= 0 or h <= 0:
            return False
        wc = self.rect().center()
        vxw = pos.x() - wc.x()
        vyw = pos.y() - wc.y()
        a = -math.radians(self.rotation_angle or 0.0)
        ca, sa = math.cos(a), math.sin(a)
        vxi = vxw * ca - vyw * sa
        vyi = vxw * sa + vyw * ca
        return (abs(vxi) <= (w * 0.5) + 0.5) and (abs(vyi) <= (h * 0.5) + 0.5)

    def _update_overlay_visibility(self, pos: Optional[QPoint] = None):
        if pos is None:
            try:
                gp = QCursor.pos()
                pos = self.mapFromGlobal(gp)
            except Exception:
                pos = self.rect().center()
        inside = self._is_pos_in_image(pos)
        # 버튼들
        self.btn_close.setVisible(inside)
        self.btn_rotate.setVisible(inside)
        # ★ 리사이즈 핸들(삼각형)도 hover 때만
        if self._show_resize_handle != inside:
            self._show_resize_handle = inside
            self.update()

    # ======= 배치 =======
    def _current_image_w_h(self):
        return self._resizing_pending_size if self._resizing_pending_size else (self.base_w, self.base_h)

    def _place_overlay_controls(self):
        w, h = self._current_image_w_h()
        pad = max(BTN_MARGIN, 2)
        # close
        close_anchor_img = (w/2 - pad - (CLOSE_BTN_W/2),
                            -h/2 + pad + (CLOSE_BTN_H/2))
        p_close = self._map_image_center_to_widget(*close_anchor_img)
        self.btn_close.move(int(p_close.x() - CLOSE_BTN_W//2),
                            int(p_close.y() - CLOSE_BTN_H//2))
        # rotate (고정 간격)
        rotate_gap = ROT_EXTRA_GAP + (CLOSE_BTN_W + ROT_BTN_W) / 2.0
        rotate_anchor_img = (w/2 - pad - rotate_gap,
                             -h/2 + pad + (ROT_BTN_H/2))
        p_rot = self._map_image_center_to_widget(*rotate_anchor_img)
        self.btn_rotate.move(int(p_rot.x() - ROT_BTN_W//2),
                             int(p_rot.y() - ROT_BTN_H//2))

        # 리사이즈 핸들 삼각형 (오른쪽-아래 모서리)
        s = self.resize_margin
        p1 = self._map_image_center_to_widget(w/2,     h/2)
        p2 = self._map_image_center_to_widget(w/2 - s, h/2)
        p3 = self._map_image_center_to_widget(w/2,     h/2 - s)
        path = QPainterPath(); path.moveTo(p1); path.lineTo(p2); path.lineTo(p3); path.closeSubpath()
        self._resize_tri = path
        self._resize_tri_pts = (p1, p2, p3)

    # ======= 마스크 =======
    def _apply_rotated_rect_mask(self):
        w, h = self._current_image_w_h()
        hw, hh = w * 0.5, h * 0.5
        corners = [QPoint(-hw, -hh), QPoint(hw, -hh), QPoint(hw, hh), QPoint(-hw, hh)]
        a = math.radians(self.rotation_angle or 0.0)
        ca, sa = math.cos(a), math.sin(a)
        cx, cy = self.rect().center().x(), self.rect().center().y()

        poly = []
        for pt in corners:
            x = pt.x()*ca - pt.y()*sa + cx
            y = pt.x()*sa + pt.y()*ca + cy
            poly.append(QPoint(int(round(x)), int(round(y))))
        region = QPolygon(poly)
        self.setMask(region)

    def _apply_rotated_rect_mask_throttled(self):
        if self._mask_pending:
            return
        self._mask_pending = True
        def _do():
            self._mask_pending = False
            self._apply_rotated_rect_mask()
            self._update_overlay_visibility()
        QTimer.singleShot(0, _do)

    # ======= 이벤트 =======
    def resizeEvent(self, e):
        self._place_overlay_controls()
        self._apply_rotated_rect_mask_throttled()
        return super().resizeEvent(e)

    def showEvent(self, e):
        super().showEvent(e)
        if self._saved_center and not self._pos_fix_applied:
            c = self._saved_center
            side = self.width()
            self.move(int(c.x() - side/2), int(c.y() - side/2))
            QTimer.singleShot(0, lambda: self.move(int(c.x() - side/2), int(c.y() - side/2)))
            self._pos_fix_applied = True

    def paintEvent(self, e):
        p = QPainter(self)
        r = self.rect()
        p.fillRect(r, QColor(0, 0, 0, 1))

        pm = self.pm_base
        if not pm.isNull():
            draw_w, draw_h = self._current_image_w_h()
            base_w, base_h = self.base_w, self.base_h
            scaling = (draw_w != base_w) or (draw_h != base_h)
            p.setRenderHint(QPainter.SmoothPixmapTransform, bool(scaling))
            p.save()
            cx, cy = r.center().x(), r.center().y()
            p.translate(cx, cy)
            if self.rotation_angle:
                p.rotate(self.rotation_angle)
            if scaling:
                p.scale(draw_w / max(1, base_w), draw_h / max(1, base_h))
            p.drawPixmap(QRect(-base_w // 2, -base_h // 2, base_w, base_h), pm)
            p.restore()

        # ★ 이미지 위 hover시에만 리사이즈 핸들 표시
        if (not self.locked) and self._resize_tri is not None and self._show_resize_handle:
            p.setOpacity(0.30)
            p.drawPath(self._resize_tri)

    # 회전
    def _begin_rotate_by_button(self):
        if self.locked:
            return
        self.rotating = True
        self.rotation_start_x = QCursor.pos().x()
        self.start_angle = self.rotation_angle
        self.grabMouse(Qt.PointingHandCursor)
        self.setCursor(Qt.PointingHandCursor)

    def _finish_rotate(self):
        if self.rotating:
            self.rotating = False
            try: self.releaseMouse()
            except Exception: pass
            self.setCursor(Qt.ArrowCursor)
            self.stateChanged.emit()
            self._update_overlay_visibility()

    # ======= 리사이즈 앵커/센터 =======
    def _image_top_left_global(self, w: int, h: int) -> QPoint:
        local = self._map_image_center_to_widget(-w/2.0, -h/2.0)
        return self.mapToGlobal(local)

    def _center_from_anchor(self, anchor_global: QPoint, w: int, h: int) -> QPoint:
        a = math.radians(self.rotation_angle or 0.0)
        ca, sa = math.cos(a), math.sin(a)
        vx, vy = -w/2.0, -h/2.0
        rx = vx * ca - vy * sa
        ry = vx * sa + vy * ca
        cx = anchor_global.x() - rx
        cy = anchor_global.y() - ry
        return QPoint(int(round(cx)), int(round(cy)))

    def _set_square_by_center_global(self, center_global: QPoint, side: int):
        x = int(round(center_global.x() - side / 2.0))
        y = int(round(center_global.y() - side / 2.0))
        self.setGeometry(x, y, side, side)

    # ======= 입력 =======
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            if self.locked:
                return
            if self._hit_test_resize(e.pos()):
                self.resizing = True
                cur_w, cur_h = self._current_image_w_h()
                self._start_base_w = cur_w
                self._start_base_h = cur_h
                self._resizing_pending_size = (cur_w, cur_h)
                self._resize_anchor_pt = self._image_top_left_global(cur_w, cur_h)
                self._scale_ema = 1.0
                self._last_applied_geom = self.geometry()
                self.setCursor(Qt.SizeFDiagCursor)
            else:
                self.dragging = True
                self.drag_start = e.globalPosition().toPoint()
                self.win_start = self.frameGeometry().topLeft()
        elif e.button() == Qt.RightButton:
            self._act_lock.setChecked(self.locked)
            self.menu.popup(e.globalPosition().toPoint())
        self._update_overlay_visibility(e.pos())

    def mouseMoveEvent(self, e):
        if self.locked:
            self.setCursor(Qt.ArrowCursor)
            self._update_overlay_visibility(e.pos())
            return

        if self.rotating:
            gx = e.globalPosition().toPoint().x()
            dx = gx - self.rotation_start_x
            self.rotation_angle = self.start_angle + dx * self.rotate_scale
            self.setUpdatesEnabled(False)
            self._place_overlay_controls()
            self._apply_rotated_rect_mask_throttled()
            self.setUpdatesEnabled(True)
            self.update()
            self._update_overlay_visibility(e.pos())
            return

        if self.resizing:
            wc = self.rect().center()
            vxw = e.pos().x() - wc.x()
            vyw = e.pos().y() - wc.y()
            a = -math.radians(self.rotation_angle or 0.0)
            ca, sa = math.cos(a), math.sin(a)
            vxi = vxw * ca - vyw * sa
            vyi = vxw * sa + vyw * ca

            w0 = max(1, self._start_base_w or self.base_w)
            h0 = max(1, self._start_base_h or self.base_h)
            bx, by = (w0 * 0.5), (h0 * 0.5)
            denom = bx*bx + by*by
            if denom <= 0:
                denom = 1
            s_raw = (vxi * bx + vyi * by) / float(denom)
            s_raw = max(0.1, s_raw)

            # 스무딩: EMA
            alpha = 0.4
            self._scale_ema = (1 - alpha) * self._scale_ema + alpha * s_raw
            s = self._scale_ema

            tmp_w = int(round(w0 * s))
            tmp_h = int(round(h0 * s))

            short = min(tmp_w, tmp_h)
            if short < MIN_SIDE:
                fix = MIN_SIDE / float(max(1, short))
                tmp_w = int(round(tmp_w * fix))
                tmp_h = int(round(tmp_h * fix))

            new_w = max(1, tmp_w)
            new_h = max(1, tmp_h)
            self._resizing_pending_size = (new_w, new_h)

            anchor = self._resize_anchor_pt or self._image_top_left_global(w0, h0)
            required_side = self._max_square_side(new_w, new_h)
            new_center_global = self._center_from_anchor(anchor, new_w, new_h)
            new_x = int(round(new_center_global.x() - required_side / 2.0))
            new_y = int(round(new_center_global.y() - required_side / 2.0))

            # 2px 임계값: 미세 변동은 건너뛰기
            g = self.geometry()
            need_apply = (
                abs(g.x() - new_x) >= 2 or
                abs(g.y() - new_y) >= 2 or
                abs(g.width() - required_side) >= 2 or
                abs(g.height() - required_side) >= 2
            )
            if need_apply:
                self.setUpdatesEnabled(False)
                self.setGeometry(new_x, new_y, required_side, required_side)
                self._place_overlay_controls()
                self._apply_rotated_rect_mask_throttled()
                self.setUpdatesEnabled(True)
                self._last_applied_geom = QRect(new_x, new_y, required_side, required_side)

            self.update()
            self._update_overlay_visibility(e.pos())
            return

        if self.dragging:
            delta = e.globalPosition().toPoint() - self.drag_start
            self.move(self.win_start + delta)
            self._update_overlay_visibility(e.pos())
            return

        self.setCursor(Qt.SizeFDiagCursor if self._hit_test_resize(e.pos()) else Qt.ArrowCursor)
        self._update_overlay_visibility(e.pos())

    def _finalize_pending_resize(self):
        if self._resizing_pending_size is None:
            return

        w_cur, h_cur = self._current_image_w_h()
        anchor = self._resize_anchor_pt or self._image_top_left_global(w_cur, h_cur)

        self.base_w, self.base_h = self._resizing_pending_size
        self.pm_base = load_pixmap_fixed(self.image_path, QSize(self.base_w, self.base_h), self.device_ratio)
        self._resizing_pending_size = None

        required_side = self._max_square_side(self.base_w, self.base_h)
        new_center_global = self._center_from_anchor(anchor, self.base_w, self.base_h)
        new_x = int(round(new_center_global.x() - required_side/2))
        new_y = int(round(new_center_global.y() - required_side/2))

        self.setUpdatesEnabled(False)
        self.setGeometry(new_x, new_y, required_side, required_side)
        self._place_overlay_controls()
        self._apply_rotated_rect_mask()
        self.setUpdatesEnabled(True)

        self.update()
        self._update_overlay_visibility()

        self._resize_anchor_pt = None
        self._scale_ema = 1.0
        self._last_applied_geom = None

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rotating:
            self._finish_rotate()
            return

        if e.button() == Qt.LeftButton:
            moved = self.dragging
            resized = self.resizing
            self.dragging = False
            if self.resizing:
                self.resizing = False
                self._finalize_pending_resize()
                self.stateChanged.emit()
            if moved:
                self.stateChanged.emit()
            if (moved or resized) and not self.locked:
                self.unsetCursor()
        self._update_overlay_visibility(e.pos())

    def enterEvent(self, e):
        self._update_overlay_visibility()
        return super().enterEvent(e)

    def leaveEvent(self, e):
        self.btn_close.setVisible(False)
        self.btn_rotate.setVisible(False)
        # hover 벗어나면 핸들도 숨김
        if self._show_resize_handle:
            self._show_resize_handle = False
            self.update()
        return super().leaveEvent(e)

    def eventFilter(self, obj, event):
        if obj in (self.btn_close, self.btn_rotate):
            if event.type() in (QEvent.Enter, QEvent.MouseMove, QEvent.Leave):
                try:
                    gp = QCursor.pos()
                    self._update_overlay_visibility(self.mapFromGlobal(gp))
                except Exception:
                    pass
        return super().eventFilter(obj, event)

    # ======= 상단/하단 플래그 =======
    def _set_topmost_flags(self, on: bool):
        self.setWindowFlag(WT("WindowStaysOnTopHint"), on)
        self.setWindowFlag(WT("WindowStaysOnBottomHint"), not on)

    def _apply_topmost(self, on: bool):
        self._set_topmost_flags(on)
        self.show()

    # 컨텍스트 메뉴
    def _on_menu(self, act):
        if act == self._act_lock:
            self.locked = not self.locked
            self._act_lock.setChecked(self.locked)
            if self.locked:
                self.dragging = False; self.resizing = False; self.rotating = False
                self.setCursor(Qt.ArrowCursor)
            self.stateChanged.emit()
        elif act == self._act_top:
            self._apply_topmost(True); self.stateChanged.emit()
        elif act == self._act_bot:
            self._apply_topmost(False); self.stateChanged.emit()
        elif act == self._act_reset:
            self.rotation_angle = 0.0
            self.setUpdatesEnabled(False)
            self._place_overlay_controls()
            self._apply_rotated_rect_mask()
            self.setUpdatesEnabled(True)
            self.update()
            self.stateChanged.emit()
            self._update_overlay_visibility()
        elif act == self._act_close:
            self.close()

    # 히트테스트
    @staticmethod
    def _pt_in_triangle(p: QPoint, a: QPoint, b: QPoint, c: QPoint) -> bool:
        x, y = p.x(), p.y()
        x1, y1 = a.x(), a.y()
        x2, y2 = b.x(), b.y()
        x3, y3 = c.x(), c.y()
        denom = ((y2 - y3)*(x1 - x3) + (x3 - x2)*(y1 - y3))
        if denom == 0:
            return False
        u = ((y2 - y3)*(x - x3) + (x3 - x2)*(y - y3)) / denom
        v = ((y3 - y1)*(x - x3) + (x1 - x3)*(y - y3)) / denom
        w = 1 - u - v
        return (u >= 0) and (v >= 0) and (w >= 0)

    def _hit_test_resize(self, pos: QPoint) -> bool:
        # ★ hover 아닐 때는 리사이즈 핸들 비활성화
        if self.locked or self._resize_tri_pts is None or not self._show_resize_handle:
            return False
        a, b, c = self._resize_tri_pts
        return self._pt_in_triangle(pos, a, b, c)

    # 직렬화 (센터 저장)
    def is_topmost(self) -> bool:
        return bool(self.windowFlags() & WT("WindowStaysOnTopHint"))

    def to_state(self) -> Dict:
        fg = self.frameGeometry()
        center = fg.center()
        return {
            "path": self.image_path,
            "x": int(self.x()), "y": int(self.y()),         # 구버전 호환
            "cx": int(center.x()), "cy": int(center.y()),   # 센터 저장
            "w": int(self.base_w), "h": int(self.base_h),
            "topmost": self.is_topmost(),
            "locked": bool(self.locked),
            "angle": float(self.rotation_angle),
        }

    def apply_state(self, st: Dict) -> None:
        if st is None:
            return
        try:
            bw = max(32, int(st.get("w", self.base_w)))
            bh = max(32, int(st.get("h", self.base_h)))
            self.base_w, self.base_h = bw, bh
            self.aspect_ratio = (bw / bh) if bh > 0 else self.aspect_ratio
            self.pm_base = load_pixmap_fixed(self.image_path, QSize(self.base_w, self.base_h), self.device_ratio)

            self.rotation_angle = float(st.get("angle", 0.0))
            topmost = bool(st.get("topmost", True))
            self.locked = bool(st.get("locked", False))

            required_side = self._max_square_side(self.base_w, self.base_h)

            if "cx" in st and "cy" in st:
                cx, cy = int(st["cx"]), int(st["cy"])
            else:
                x = int(st.get("x", self.x()))
                y = int(st.get("y", self.y()))
                cx, cy = x + required_side // 2, y + required_side // 2

            self._set_topmost_flags(topmost)
            self.setGeometry(int(cx - required_side/2), int(cy - required_side/2),
                             required_side, required_side)
            self._saved_center = QPoint(cx, cy)
            self._pos_fix_applied = False

            self._place_overlay_controls()
            self._apply_rotated_rect_mask()
            self._update_overlay_visibility()
        except Exception:
            pass


# ================== Toolbar ==================
class StickerToolbar(QMainWindow):
    __slots__ = ("btn_add", "btn_quit", "_drag", "_drag_start", "_win_start",
                 "tray", "stickers", "save_manager",
                 "_act_autorun", "_act_toolbar_show")

    def __init__(self, save_manager: SaveManager):
        super().__init__()
        self.save_manager = save_manager
        self.setWindowFlags(Qt.Tool | WT("FramelessWindowHint"))
        self.setWindowTitle("Sticker Board")
        self.setMinimumSize(160, 56)
        self.setWindowOpacity(0.96)

        root = QWidget(self)
        self.setCentralWidget(root)
        h = QHBoxLayout(root); h.setContentsMargins(10, 8, 10, 8); h.setSpacing(8)

        self.btn_add = QToolButton(self); self.btn_add.setText("+"); self.btn_add.setToolTip("이미지 추가")
        self.btn_add.setStyleSheet("QToolButton{padding:6px 10px;font-weight:bold;}")
        self.btn_add.clicked.connect(self.pick_images)

        self.btn_quit = QToolButton(self); self.btn_quit.setText("⎋"); self.btn_quit.setToolTip("모두 종료")
        self.btn_quit.setStyleSheet("QToolButton{padding:6px 10px;font-weight:bold;}")
        self.btn_quit.clicked.connect(self._quit_app)

        h.addWidget(self.btn_add); h.addWidget(self.btn_quit)

        self._drag = False
        self.stickers: List[StickerWindow] = []
        self.resize(200, 60); self.move(80, 80)

        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(self.style().standardIcon(QStyle.SP_ComputerIcon))
        menu = QMenu()

        self._act_toolbar_show = menu.addAction("메뉴바 표시")
        self._act_toolbar_show.setCheckable(True); self._act_toolbar_show.setChecked(True)
        def _toggle_toolbar_show(checked: bool):
            if checked:
                self._apply_toolbar_desktop(); self.show(); self.lower()
            else:
                self.hide()
        self._act_toolbar_show.toggled.connect(_toggle_toolbar_show)

        menu.addSeparator()
        self._act_add = menu.addAction("스티커 추가", self.pick_images)

        menu.addSeparator()
        self._act_autorun = menu.addAction("시작 시 자동 실행")
        self._act_autorun.setCheckable(True)
        self._act_autorun.setChecked(_win_is_startup_enabled(APP_RUN_VALUE))
        def _toggle_autorun():
            new_state = not _win_is_startup_enabled(APP_RUN_VALUE)
            ok = _win_set_startup(APP_RUN_VALUE, new_state)
            self._act_autorun.setChecked(ok and new_state)
        self._act_autorun.triggered.connect(_toggle_autorun)

        menu.addSeparator()
        menu.addAction("종료", self._quit_app)
        self.tray.setContextMenu(menu); self.tray.show()

        QApplication.instance().aboutToQuit.connect(self._on_about_to_quit)

    def _apply_toolbar_desktop(self):
        self.setWindowFlag(WT("WindowStaysOnTopHint"), False)
        self.setWindowFlag(WT("WindowStaysOnBottomHint"), True)
        self.show(); self.lower()

    def _on_about_to_quit(self):
        global IS_EXITING
        IS_EXITING = True

    def _quit_app(self):
        self._on_about_to_quit()
        QApplication.instance().quit()

    # 드래그 이동(툴바 이동은 저장하지 않음)
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = True
            self._drag_start = e.globalPosition().toPoint()
            self._win_start = self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag:
            delta = e.globalPosition().toPoint() - self._drag_start
            self.move(self._win_start + delta)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = False

    def showEvent(self, e):
        try: self._act_toolbar_show.setChecked(True)
        except Exception: pass
        self._apply_toolbar_desktop()
        return super().showEvent(e)

    def hideEvent(self, e):
        try: self._act_toolbar_show.setChecked(False)
        except Exception: pass
        return super().hideEvent(e)

    # ----- 스티커 관리 / 저장 트리거 -----
    def _apply_cache_budget(self):
        n = max(1, len(self.stickers))
        mb = min(64, max(16, n * 3))
        try:
            QPixmapCache.setCacheLimit(mb * 1024)  # KB
        except Exception:
            pass

    def pick_images(self):
        files, _ = QFileDialog.getOpenFileNames(self, "이미지 선택", "", "Images (*.png *.jpg *.jpeg *.bmp *.webp)")
        if not files:
            return
        x, y = self.x(), self.y() + self.height() + 10
        for i, f in enumerate(files):
            self.create_sticker(f, QPoint(x + i * 28, y + i * 28))
        self.save_all()

    def create_sticker(self, path: str, pos: Optional[QPoint] = None, *, show_now: bool = True, initial_topmost: bool = True) -> Optional[StickerWindow]:
        if not path or not os.path.exists(path):
            return None
        s = StickerWindow(path, start_pos=pos, initial_topmost=initial_topmost)
        s.stateChanged.connect(self.save_all)
        s.destroyed.connect(lambda *_: self._cleanup_and_save(s))
        if show_now:
            s.show()
        self.stickers.append(s)
        self._apply_cache_budget()
        return s

    def _cleanup_and_save(self, s: StickerWindow):
        try: self.stickers.remove(s)
        except ValueError: pass
        self._apply_cache_budget()
        self.save_all()

    def build_state(self) -> Dict:
        return {
            "version": 20,  # 핸들 hover 표시
            "toolbar": {
                "x": int(self.x()), "y": int(self.y()),
                "visible": not self.isHidden(),
            },
            "stickers": [st.to_state() for st in self.stickers if isinstance(st, StickerWindow)],
        }

    def save_all(self):
        if IS_EXITING:
            return
        self.save_manager.save(self.build_state())

    def restore_from_save(self):
        data = self.save_manager.load()
        if not data:
            return
        try:
            tb = data.get("toolbar", {})
            if "x" in tb and "y" in tb:
                self.move(int(tb.get("x", self.x())), int(tb.get("y", self.y())))
            vis = bool(tb.get("visible", True))
            (self.show() if vis else self.hide())
            try: self._act_toolbar_show.setChecked(vis)
            except Exception: pass

            stickers = data.get("stickers", [])
            cleaned_stickers = []
            for st in stickers:
                path = st.get("path", "")
                if not path or not os.path.exists(path):
                    continue
                s = self.create_sticker(path, None, show_now=False, initial_topmost=False)
                if s is None:
                    continue
                s.apply_state(st)
                s.show()
                cleaned_stickers.append(st)

            if len(cleaned_stickers) != len(stickers):
                data["stickers"] = cleaned_stickers
                self.save_manager.save(data)

        except Exception:
            pass


# ================== Main ==================
def main():
    try:
        QPixmapCache.setCacheLimit(16 * 1024)  # 16MB (KB)
    except Exception:
        pass

    QApplication.setAttribute(Qt.AA_CompressHighFrequencyEvents, True)

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    sm = SaveManager()

    # Robust Singleton
    lock_path = os.path.join(sm.base, "StickerBoard.lock")
    lock = QLockFile(lock_path)
    lock.setStaleLockTime(30000)
    if not lock.tryLock(1):
        lock.removeStaleLockFile()
        if not lock.tryLock(1):
            sys.exit(0)

    try:
        QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
            QGuiApplication.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
    except Exception:
        pass

    w = StickerToolbar(sm)
    w.restore_from_save()
    w.show()

    if len(sys.argv) > 1:
        base = w.pos() + QPoint(30, 60)
        for i, pth in enumerate(sys.argv[1:]):
            if not os.path.exists(pth):
                continue
            _ = w.create_sticker(pth, base + QPoint(28 * i, 28 * i))
        w.save_all()

    code = app.exec()
    try:
        lock.unlock()
    except Exception:
        pass
    sys.exit(code)


if __name__ == "__main__":
    main()
