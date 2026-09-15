"""标注与打码（M6）：独立图层 JSON 模型 + 撤销重做 + 导出时栅格化叠加。

原图永不修改：标注仅存 layers_json；仅导出/预览时在内存中栅格化。
图元类型：pen（画笔）/ arrow / rect / ellipse / text / mosaic（马赛克）/ blur（高斯模糊）。
"""
from __future__ import annotations

import copy
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from .exceptions import IntegrityError
from .util import atomic_write_text

LAYER_VERSION = 1
MAX_UNDO = 20  # 撤销重做 >= 20 步

ELEMENT_TYPES = {"pen", "arrow", "rect", "ellipse", "text", "mosaic", "blur"}


@dataclass
class Element:
    etype: str
    points: list[tuple[float, float]] = field(default_factory=list)
    color: str = "#ff0000"
    stroke_width: int = 3
    text: str = ""
    font_size: int = 24
    rect: tuple[float, float, float, float] | None = None  # x, y, w, h
    strength: float = 0.7          # 打码强度 0-1
    eid: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def to_dict(self) -> dict:
        return {
            "eid": self.eid,
            "type": self.etype,
            "points": [[float(x), float(y)] for x, y in self.points],
            "color": self.color,
            "stroke_width": self.stroke_width,
            "text": self.text,
            "font_size": self.font_size,
            "rect": list(self.rect) if self.rect else None,
            "strength": self.strength,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Element":
        etype = d.get("type", "")
        if etype not in ELEMENT_TYPES:
            raise IntegrityError(f"未知图元类型: {etype}")
        rect = d.get("rect")
        return cls(
            etype=etype,
            points=[(float(p[0]), float(p[1])) for p in d.get("points", [])],
            color=d.get("color", "#ff0000"),
            stroke_width=int(d.get("stroke_width", 3)),
            text=str(d.get("text", "")),
            font_size=int(d.get("font_size", 24)),
            rect=tuple(float(v) for v in rect) if rect else None,
            strength=float(d.get("strength", 0.7)),
            eid=str(d.get("eid") or uuid.uuid4().hex[:12]),
        )


class AnnotationModel:
    """可撤销重做的标注图层模型。"""

    def __init__(self, width: int = 0, height: int = 0,
                 elements: list[Element] | None = None):
        self.width = width
        self.height = height
        self._elements: list[Element] = elements or []
        self._undo: list[list[Element]] = []
        self._redo: list[list[Element]] = []

    # ------------------------------------------------------------------
    @property
    def elements(self) -> list[Element]:
        return list(self._elements)

    def _snapshot(self) -> list[Element]:
        return copy.deepcopy(self._elements)

    def _commit(self, snapshot: list[Element]) -> None:
        self._undo.append(snapshot)
        if len(self._undo) > MAX_UNDO:
            self._undo.pop(0)
        self._redo.clear()

    # ------------------------------------------------------------------
    def add(self, el: Element) -> None:
        self._commit(self._snapshot())
        self._elements.append(el)

    def update(self, eid: str, **kwargs) -> Element | None:
        for el in self._elements:
            if el.eid == eid:
                self._commit(self._snapshot())
                for k, v in kwargs.items():
                    setattr(el, k, v)
                return el
        return None

    def remove(self, eid: str) -> bool:
        for i, el in enumerate(self._elements):
            if el.eid == eid:
                self._commit(self._snapshot())
                self._elements.pop(i)
                return True
        return False

    def clear(self) -> None:
        if self._elements:
            self._commit(self._snapshot())
            self._elements.clear()

    # ------------------------------------------------------------------
    def undo(self) -> bool:
        if not self._undo:
            return False
        self._redo.append(self._snapshot())
        self._elements = self._undo.pop()
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        self._undo.append(self._snapshot())
        self._elements = self._redo.pop()
        return True

    def undo_depth(self) -> int:
        return len(self._undo)

    def redo_depth(self) -> int:
        return len(self._redo)

    # ------------------------------------------------------------------
    def to_json(self, indent: int | None = None) -> str:
        return json.dumps({
            "version": LAYER_VERSION,
            "width": self.width,
            "height": self.height,
            "elements": [e.to_dict() for e in self._elements],
        }, ensure_ascii=False, indent=indent)

    @classmethod
    def from_json(cls, raw: str, width: int = 0, height: int = 0) -> "AnnotationModel":
        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            raise IntegrityError(f"标注 JSON 损坏: {exc}") from exc
        if not isinstance(data, dict):
            raise IntegrityError("标注 JSON 结构错误")
        model = cls(
            width=int(data.get("width", width) or width),
            height=int(data.get("height", height) or height),
            elements=[Element.from_dict(e) for e in data.get("elements", [])],
        )
        return model


# ======================================================================
# 栅格化（导出/预览时才执行；原图文件只读）
# ======================================================================
def _color(c: str, alpha: int = 255) -> tuple:
    c = c.lstrip("#")
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), alpha)


def rasterize(image_path: str, model: AnnotationModel, font_path: str | None = None) -> Any:
    """在内存中把图层叠加到原图，返回新的 PIL Image（不写回原文件）。"""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    img = Image.open(image_path).convert("RGB")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    font = None
    if font_path:
        try:
            font = ImageFont.truetype(font_path, 24)
        except OSError:
            font = None

    for el in model.elements:
        try:
            if el.etype == "pen" and len(el.points) >= 2:
                draw.line(el.points, fill=_color(el.color), width=el.stroke_width, joint="curve")
            elif el.etype == "arrow" and len(el.points) >= 2:
                _draw_arrow(draw, el)
            elif el.etype == "rect" and el.rect:
                x, y, w, h = el.rect
                draw.rectangle([x, y, x + w, y + h], outline=_color(el.color),
                               width=el.stroke_width)
            elif el.etype == "ellipse" and el.rect:
                x, y, w, h = el.rect
                draw.ellipse([x, y, x + w, y + h], outline=_color(el.color),
                             width=el.stroke_width)
            elif el.etype == "text":
                f = ImageFont.truetype(font_path, el.font_size) if font_path else font
                x, y = el.points[0] if el.points else (0, 0)
                draw.text((x, y), el.text, fill=_color(el.color), font=f or ImageFont.load_default())
            elif el.etype in ("mosaic", "blur") and el.rect:
                x, y, w, h = el.rect
                x, y, w, h = max(0, int(x)), max(0, int(y)), max(1, int(w)), max(1, int(h))
                region = img.crop((x, y, x + w, y + h))
                if el.etype == "mosaic":
                    cell = max(1, int(el.strength * 20))
                    region = region.resize(
                        (max(1, w // cell), max(1, h // cell)),
                        Image.Resampling.NEAREST,
                    ).resize((w, h), Image.Resampling.NEAREST)
                else:
                    region = region.filter(ImageFilter.GaussianBlur(
                        radius=max(1.0, el.strength * 20)))
                overlay.paste(region.convert("RGBA"), (x, y))
        except (ValueError, TypeError, OSError):
            continue  # 单个图元渲染失败不影响整体

    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")


def _draw_arrow(draw, el) -> None:
    """箭头：直线 + 箭头头部（按最后一段方向）。"""
    import math

    pts = el.points
    x1, y1 = pts[-2]
    x2, y2 = pts[-1]
    draw.line([(x1, y1), (x2, y2)], fill=_color(el.color), width=el.stroke_width)
    angle = math.atan2(y2 - y1, x2 - x1)
    head = max(8, el.stroke_width * 4)
    for da in (math.radians(150), math.radians(210)):
        dx, dy = math.cos(angle + da) * head, math.sin(angle + da) * head
        draw.line([(x2, y2), (x2 + dx, y2 + dy)], fill=_color(el.color),
                  width=max(2, el.stroke_width // 2))


# ======================================================================
# 标注存储服务（annotations 表）
# ======================================================================
class AnnotationStore:
    def __init__(self, db, config):
        self.db = db
        self.config = config

    def get_model(self, asset_id: int, image_size: tuple[int, int] | None = None) -> AnnotationModel:
        raw = self.db.scalar("SELECT layers_json FROM annotations WHERE asset_id=?", (asset_id,))
        w, h = image_size or (0, 0)
        if raw is None:
            return AnnotationModel(width=w, height=h)
        return AnnotationModel.from_json(raw, width=w, height=h)

    def save_model(self, asset_id: int, model: AnnotationModel) -> None:
        from .timeutil import utc_now

        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO annotations(asset_id, layers_json, updated_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(asset_id) DO UPDATE SET layers_json=excluded.layers_json, "
                "updated_at=excluded.updated_at",
                (asset_id, model.to_json(), utc_now()),
            )

    def delete(self, asset_id: int) -> None:
        with self.db.tx() as conn:
            conn.execute("DELETE FROM annotations WHERE asset_id=?", (asset_id,))
