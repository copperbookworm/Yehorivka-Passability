# -*- coding: utf-8 -*-
"""Squad 间瞄火力计算内核：网格键位 + 弹道解算（与 Yehorivka 页面同口径）。

公式与参数全部来自 SquadCalc 46.1.0（MIT-NC, sh4rkman）的弹道解算，
本文件只把它从 JS 逐行搬到 Python，并沿用页面里已核对过的实现细节。
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import paths

DATA = paths.resource_dir() / "data"

KP_M = 300.0          # 主格边长（米）
KP_LEVELS = 3         # 主格 + 两级子格
G0 = 9.78             # SquadCalc App.gravity


# ---------------------------------------------------------------- 网格键位
def col_letter(col: int) -> str:
    s = ""
    while col >= 0:
        s = chr(65 + col % 26) + s
        col = col // 26 - 1
    return s


def letter_col(s: str) -> int:
    c = 0
    for ch in s:
        c = c * 26 + (ord(ch) - 64)
    return c - 1


def text_at(gx: float, gz: float, level: int = 3) -> str:
    """相对地图左上角的米数 -> 网格文本（D4-5-7）。"""
    out = col_letter(int(math.floor(gx / KP_M))) + str(int(math.floor(gz / KP_M)) + 1)
    for k in range(1, min(level, KP_LEVELS)):
        iv = KP_M / (3 ** k)
        sx = int(math.floor(gx / iv)) % 3
        sz = int(math.floor(gz / iv)) % 3
        out += "-" + str(7 - sz * 3 + sx)
    return out


class ParseError(ValueError):
    pass


def parse(text: str) -> tuple[float, float]:
    """网格文本 -> 相对米数（格心）。支持 D3 / D3-7 / D3-7-4。"""
    import re
    raw = str(text or "").strip()
    if not raw:
        raise ParseError("没有坐标")
    t = re.sub(r"[^A-Z0-9]+", "-", raw.upper()).strip("-")
    m = re.match(r"^([A-Z]+)0*(\d+)((?:-\d+)*)$", t)
    if not m:
        raise ParseError("格式不对，应形如 D4-5-7")
    col = letter_col(m.group(1))
    if col < 0:
        raise ParseError("列字母不合法")
    row = int(m.group(2)) - 1
    if row < 0:
        raise ParseError("行号要从 1 开始")
    gx, gz = col * KP_M, row * KP_M
    rest = [p for p in (m.group(3).split("-")[1:] if m.group(3) else []) if p != ""]
    if len(rest) > KP_LEVELS - 1:
        raise ParseError("最多两级子格")
    iv = KP_M
    for i, p in enumerate(rest):
        if not re.match(r"^0*[1-9]$", p):
            raise ParseError("子格只能是 1–9 的单个数字")
        iv = KP_M / (3 ** (i + 1))
        n = int(p)
        gx += iv * ((n - 1) % 3)
        gz += iv * (2 - (n - 1) // 3)
    gx += iv / 2
    gz += iv / 2
    return gx, gz


# ---------------------------------------------------------------- 弹道
def load_weapons() -> list[dict]:
    return json.loads((DATA / "weapons.json").read_text(encoding="utf-8"))


def load_maps() -> list[dict]:
    return json.loads((DATA / "maps.json").read_text(encoding="utf-8"))


def velocity(w: dict, d: float) -> float:
    dec = w.get("dec") or 0
    if not dec or d <= 0:
        return w["v"]
    dec_t = w["decT"]
    d_dist = w["v"] * dec_t - 0.5 * dec * dec_t * dec_t
    if d <= d_dist:
        t = (math.sqrt(w["v"] * w["v"] + 2 * dec * d) - w["v"]) / dec
        return w["v"] - dec * t
    fv = w["v"] - dec * dec_t
    return d / (dec_t + (d - d_dist) / fv)


def solve(w: dict, dist: float, dh: float) -> dict:
    """返回 {low, high, err}；每个角档含 rad/deg/mil/tof/sprA/sprB/ok/why。"""
    g = G0 * w.get("gs", 1)
    v = velocity(w, dist)
    moa_rad = math.radians(w["moa"] / 60.0)
    out = {"dist": dist, "dh": dh, "v": v, "low": None, "high": None, "err": ""}
    if dist < 0.5:
        out["err"] = "炮位与目标重合"
        return out
    p1 = v ** 4 - g * (g * dist * dist + 2 * (dh - w.get("hOff", 0)) * v * v)
    if p1 < 0:
        out["err"] = "超出射程"
        return out
    P = math.sqrt(p1)
    for key, sgn in (("low", -1.0), ("high", 1.0)):
        el = math.atan((v * v + sgn * P) / (g * dist)) - math.radians(w.get("aOff") or 0)
        o = {"key": key, "rad": el, "deg": math.degrees(el),
             "mil": math.degrees(el) / (360.0 / 6400.0),
             "tof": 0.0, "ok": True, "why": "", "sprA": 0.0, "sprB": 0.0}
        lo, hi = w["minEl"]
        if not math.isfinite(el) or o["deg"] < lo or o["deg"] > hi:
            o["ok"] = False
            o["why"] = f"超出射界 {lo:g}–{hi:g}°"
        else:
            vs = v * math.sin(el)
            inner = vs * vs + 2 * g * (-dh)
            tof = (vs + math.sqrt(inner)) / g if inner >= 0 else float("nan")
            if not math.isfinite(tof):
                tof = (vs + abs(vs)) / g
            o["tof"] = tof
            if not math.isfinite(tof) or tof > (w.get("life") or 100):
                o["ok"] = False
                o["why"] = "弹丸寿命外"
            o["sprA"] = max(0.0, math.sin(moa_rad) * v * tof)
            r1 = v * v * math.sin(2 * (el + moa_rad / 2)) / g
            r2 = v * v * math.sin(2 * (el - moa_rad / 2)) / g
            o["sprB"] = abs(r1 - r2)
        out[key] = o
    return out


def evaluate(w: dict, gun_rel: tuple[float, float], tgt_rel: tuple[float, float],
             h_gun: float, h_tgt: float) -> dict:
    """gun_rel / tgt_rel 是相对地图左上角的米数。"""
    res = {"dist": 0.0, "dh": 0.0, "hGun": h_gun, "hTgt": h_tgt,
           "brg": 0.0, "milBrg": 0.0, "sol": None, "solName": "", "err": "", "warnMin": False}
    dx = tgt_rel[0] - gun_rel[0]
    dz = tgt_rel[1] - gun_rel[1]
    dist = math.hypot(dx, dz)
    res["dist"] = dist
    res["dh"] = h_tgt - h_gun
    brg = math.degrees(math.atan2(dx, -dz))
    if brg < 0:
        brg += 360
    res["brg"] = brg
    res["milBrg"] = brg / (360.0 / 6400.0)
    if dist < 1:
        res["err"] = "与阵地重合"
        return res
    sol = solve(w, dist, res["dh"])
    order = ("low", "high") if w.get("ang") == "low" else ("high", "low")
    pick, last = None, None
    for key in order:
        o = sol.get(key)
        last = o
        if o and o["ok"]:
            pick = o
            break
    if pick is None:
        res["err"] = sol.get("err") or (last or {}).get("why") or "无有效解"
        return res
    res["sol"] = pick
    res["solName"] = "高角" if pick["key"] == "high" else "低角"
    res["warnMin"] = dist < (w.get("minD") or 0)
    return res


def angle_text(w: dict, deg: float) -> str:
    if w.get("unit") == "mil":
        return f"{deg / (360.0 / 6400.0):.1f} mil"
    if w.get("unit") == "degMin":
        sign = "-" if deg < 0 else ""
        a = abs(deg)
        dd = int(a // 1)
        mm = int(round((a - dd) * 60))
        if mm == 60:
            dd += 1
            mm = 0
        return f"{sign}{dd}°{mm:02d}′"
    return f"{deg:.1f}°"
