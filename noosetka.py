#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
НООСЕТКА · ПУЛЬТ МИРОВ — бейдж-виджет дома rtynbirf.

REVE: сначала LAYOUT (layout.json), потом RENDER (SVG-пульт + живой HTML).
Закон дома: факт = байты — состояние мира читается из committed noo/badge.json
(бейдж не может соврать), пульт вымеряется, не декларируется.

Ступени: GHOST → WIRE → LIVE → READY; отказ → DEGRADED (с причиной);
архив → SEALED. Брайль-строка дома — канон latin-g1.

CLI:
  python3 noosetka.py all      # промер 81 мира → noo/snapshot.json → noosetka.svg + index.html
  python3 noosetka.py probe    # только промер
  python3 noosetka.py render   # только рендер из снимка
  python3 noosetka.py verify   # сверка снимка и пульта
Опции: --registry ФАЙЛ (реестр миров локально), --profile ФАЙЛ (README профиля локально),
       --out КАТАЛОГ, --workers N
"""
import argparse
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.sax.saxutils as sx
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

OWNER = "rtynbirf"
REGISTRY_URL = "https://raw.githubusercontent.com/%s/singulyar/rumu/noo/миры.json" % OWNER
PROFILE_URL = "https://raw.githubusercontent.com/%s/rtynbirf/main/README.md" % OWNER
RAW_BASE = "https://raw.githubusercontent.com"

STATES = ("READY", "LIVE", "WIRE", "GHOST", "DEGRADED", "SEALED")
# порядок «готовности» мира: хуже = больше; SEALED не роняет дом
RANK = {"READY": 0, "SEALED": 1, "LIVE": 2, "WIRE": 3, "GHOST": 4, "DEGRADED": 5}
STATE_LABEL = {
    "READY": "готов",
    "LIVE": "жив",
    "WIRE": "на проводе",
    "GHOST": "призрак — манифеста нет",
    "DEGRADED": "отказ",
    "SEALED": "опечатан (архив)",
}
CANON = "factum"  # факт = байты

# Брайль latin-g1: биты точек 1..6 (dot1=1, dot2=2, dot3=4, dot4=8, dot5=16, dot6=32)
BRAILLE = {
    "a": 0x01, "b": 0x03, "c": 0x09, "d": 0x19, "e": 0x11, "f": 0x0B,
    "g": 0x1B, "h": 0x13, "i": 0x0A, "j": 0x1A, "k": 0x05, "l": 0x07,
    "m": 0x0D, "n": 0x1D, "o": 0x15, "p": 0x0F, "q": 0x1F, "r": 0x17,
    "s": 0x0E, "t": 0x1E, "u": 0x25, "v": 0x27, "w": 0x3A, "x": 0x2D,
    "y": 0x3D, "z": 0x35,
}
# точки 1..6: (колонка, строка) в ячейке 2×3 (кол: 0 левый, 1 правый)
DOT_POS = {0: (0, 0), 1: (0, 1), 2: (0, 2), 3: (1, 0), 4: (1, 1), 5: (1, 2)}

UA = "noosetka/1.0 (+https://github.com/rtynbirf)"


def ascii_url(url):
    """URL только из ASCII: кириллицу пути в %XX (UTF-8), готовые %XX не трогаем.
    Локаль раннера (POSIX/ascii) не должна ломать промер — кросс-машинный
    детерминизм: один и тот же промер на любой машине."""
    return urllib.parse.quote(url, safe="/:?&=#%")


def http_get(url, timeout=12):
    """(status|None, bytes, ms, err|None) — ни одного выдуманного числа."""
    req = urllib.request.Request(ascii_url(url), headers={"User-Agent": UA})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return resp.status, body, int((time.perf_counter() - t0) * 1000), None
    except urllib.error.HTTPError as e:
        return e.code, b"", int((time.perf_counter() - t0) * 1000), "HTTP %s" % e.code
    except Exception as e:  # сеть/таймаут/DNS — честная причина
        return None, b"", int((time.perf_counter() - t0) * 1000), "%s: %s" % (type(e).__name__, e)


def braille_char(ch):
    return chr(0x2800 | BRAILLE[ch.lower()])


def braille_word(word):
    out = []
    for ch in word.lower():
        if ch in BRAILLE:
            out.append(braille_char(ch))
    return "".join(out)


def braille_bits(ch):
    code = ord(ch) - 0x2800
    return [(code >> i) & 1 for i in range(6)]


# ---------------------------------------------------------------- источники

def load_registry(path=None):
    """Реестр миров — канон singulyar@rumu noo/миры.json (ссылка живёт в профиле)."""
    if path:
        raw = Path(path).read_bytes()
    else:
        status, raw, _, err = http_get(REGISTRY_URL)
        if status != 200:
            raise SystemExit("реестр миров недоступен (%s): %r" % (REGISTRY_URL, err))
    data = json.loads(raw.decode("utf-8"))
    worlds = []
    for it in data.get("миры", []):
        worlds.append({
            "repo": str(it["репо"]),
            "branch": str(it.get("ветка", "main") or "main"),
            "role": str(it.get("роль", "") or ""),
            "braille": str(it.get("брайль", "") or ""),
            "sealed": bool(it.get("опечатан") or it.get("архив") or it.get("sealed")),
            "order": int(it.get("порядок", 0) or 0),
        })
    worlds.sort(key=lambda w: (w["order"], w["repo"].lower()))
    return worlds


ROW_RE = re.compile(
    r"^\|\s*\[`([^`]+)`\]\(https://github\.com/[^)]+\)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*$"
)


def parse_profile_sealed(text):
    """Меты SEALED из committed таблицы профиля: средняя ячейка без бейджа, с меткой SEALED."""
    sealed = set()
    for line in text.splitlines():
        m = ROW_RE.match(line)
        if not m:
            continue
        mid = m.group(2)
        if "SEALED" in mid and "img.shields.io" not in mid:
            sealed.add(m.group(1))
    return sealed


def load_profile_sealed(path=None):
    if path:
        text = Path(path).read_text(encoding="utf-8")
    else:
        status, body, _, _ = http_get(PROFILE_URL)
        if status != 200:
            return set()
        text = body.decode("utf-8", "replace")
    return parse_profile_sealed(text)


# ---------------------------------------------------------------- промер

def probe(world, base=None):
    """Один мир → один факт: state/reason/ms/badge_sha12. Без выдумок."""
    repo, branch = world["repo"], world["branch"]
    seed = {
        "repo": repo, "branch": branch,
        "role": world.get("role", ""), "braille": world.get("braille", ""),
        "ms": None, "badge_sha12": None,
    }
    if world.get("sealed"):
        seed.update(state="SEALED",
                    reason="архив (заморожен владельцем, запись закрыта GitHub)")
        return seed
    url = "%s/%s/%s/%s/noo/badge.json" % (
        base or RAW_BASE, OWNER, urllib.parse.quote(repo, safe=""),
        urllib.parse.quote(branch, safe=""))
    status, body, ms, err = http_get(url)
    seed["ms"] = ms
    if status == 200:
        seed["badge_sha12"] = hashlib.sha256(body).hexdigest()[:12]
        try:
            badge = json.loads(body.decode("utf-8"))
        except Exception as e:
            seed.update(state="DEGRADED", reason="badge.json не парсится: %s" % e)
            return seed
        msg = str(badge.get("message", "") or "").strip()
        head = msg.split("·")[0].strip().split(" ")[0].upper() if msg else ""
        if head in STATES:
            seed.update(state=head, reason=msg or "(без сообщения)")
        else:
            seed.update(state="DEGRADED", reason="message непонятен: %r" % msg[:60])
        return seed
    if status == 404:
        seed.update(state="GHOST", reason="noo/badge.json: 404 — манифеста нет")
        return seed
    seed.update(state="DEGRADED", reason=err or ("HTTP %s" % status))
    return seed


def measure(worlds, workers=8):
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(probe, worlds))
    return results


def post_archive_check(results, cap=10):
    """GHOST → SEALED, если GitHub говорит archived=true (не больше cap проверок)."""
    out, checked = [], 0
    for r in results:
        if r["state"] == "GHOST" and checked < cap:
            checked += 1
            status, body, _, _ = http_get("https://api.github.com/repos/%s/%s" % (OWNER, r["repo"]))
            if status == 200:
                try:
                    if json.loads(body.decode("utf-8")).get("archived"):
                        r = dict(r)
                        r["state"] = "SEALED"
                        r["reason"] = "архив (заморожен владельцем, запись закрыта GitHub)"
                except Exception:
                    pass
        out.append(r)
    return out


def aggregate(results):
    """Дом = худшая ступень среди живых; SEALED не роняет; DEGRADED — дом в отказе."""
    states = [r["state"] for r in results]
    if not states:
        return "GHOST"
    cand = [s for s in states if s != "SEALED"]
    if not cand:
        return "SEALED"
    return max(cand, key=lambda s: RANK[s])


def counters(results):
    c = {}
    for r in results:
        c[r["state"]] = c.get(r["state"], 0) + 1
    return {s: c[s] for s in STATES if s in c}


def cnt_line(c):
    return " · ".join("%s %d" % (s, c[s]) for s in STATES if s in c) or "мир пуст"


# ---------------------------------------------------------------- REVE: render

def esc(s):
    return sx.escape(str(s), {"\"": "&quot;"})


def _num(v):
    s = ("%.2f" % v).rstrip("0").rstrip(".")
    return s if s else "0"


def braille_svg(ch, x, y, u, r, on, off, off_opacity=0.18):
    """Один брайль-знак как точки-круги (не зависим от шрифтов — канон виден всегда)."""
    if not (ch and 0x2800 <= ord(ch) <= 0x28FF):
        return "", 0.0
    bits = braille_bits(ch)
    parts = []
    for dot in range(6):
        col, row = DOT_POS[dot]
        cx = x + col * u + r
        cy = y + row * u + r
        if bits[dot]:
            parts.append('<circle cx="%s" cy="%s" r="%s" fill="%s"/>' % (_num(cx), _num(cy), _num(r), on))
        else:
            parts.append('<circle cx="%s" cy="%s" r="%s" fill="%s" fill-opacity="%s"/>'
                         % (_num(cx), _num(cy), _num(r * 0.55), off, _num(off_opacity)))
    width = u + 2 * r
    return "".join(parts), width


def render_svg(layout, results, stamp):
    """REVE: layout → SVG-пульт. Детерминизм: те же входы — те же байты."""
    P, C = layout["palette"], layout["canvas"]
    G, BR, PN = layout["grid"], layout["braille"], layout["panel"]
    font = layout["font"]
    w, pad = C["w"], C["pad"]

    n = len(results)
    cell, gap, cols = G["cell"], G["gap"], G["cols"]
    rows = (n + cols - 1) // cols
    grid_w = cols * cell + (cols - 1) * gap
    grid_h = rows * cell + (rows - 1) * gap
    gx, gy = pad, 124

    px0 = pad + grid_w + 42
    panel_w = w - px0 - pad
    lh = PN["line_h"]
    cnt0 = counters(results)
    panel = []  # (state, text) — ступени дома с примерами миров
    for s in STATES:
        if s not in cnt0:
            continue
        ex = [r["repo"] for r in results if r["state"] == s][:3]
        more = cnt0[s] - len(ex)
        panel.append((s, "%s %d · %s — %s%s" % (s, cnt0[s], STATE_LABEL[s],
                      ", ".join(ex), (" +%d ещё" % more) if more > 0 else "")))
    trouble = [r for r in results if r["state"] in ("DEGRADED", "GHOST")]
    tlines = []  # (state, text) — отказы и призраки с причинами
    for r in trouble[:PN["cap"]]:
        ms = " · %dms" % r["ms"] if r["ms"] is not None else ""
        tlines.append((r["state"], "%s — %s%s" % (r["repo"], r["reason"], ms)))
    textra = len(trouble) - len(tlines)
    if textra > 0:
        tlines.append(("GHOST", "+%d ещё — на живом пульте" % textra))
    panel_h = 20 + len(panel) * lh + 30 + max(1, len(tlines)) * lh
    grid_bottom = gy + grid_h
    H = int(max(grid_bottom, gy + panel_h) + 56)

    home = aggregate(results)
    cnt = counters(results)
    hp = home in P and P[home]

    out = []
    out.append('<?xml version="1.0" encoding="UTF-8"?>')
    out.append('<svg xmlns="http://www.w3.org/2000/svg" width="%s" height="%d" '
               'viewBox="0 0 %s %d" role="img" aria-label="НООСЕТКА · ПУЛЬТ МИРОВ">' % (w, H, w, H))
    out.append('<rect width="%d" height="%d" rx="%s" fill="%s"/>' % (w, H, C["radius"], P["bg"]))
    out.append('<rect x="0.5" y="0.5" width="%s" height="%s" rx="%s" fill="none" stroke="%s"/>'
               % (_num(w - 1), _num(H - 1), C["radius"], C["border"]))

    # зона 1: титул + девиз
    out.append('<text x="%d" y="44" font-family="%s" font-size="21" font-weight="bold" fill="%s">%s</text>'
               % (pad, esc(font), P["text"], esc(layout["title"])))
    out.append('<text x="%s" y="44" text-anchor="end" font-family="%s" font-size="11" fill="%s">%s</text>'
               % (w - pad, esc(font), P["faint"], esc(layout["motto"])))

    # зона 2: брайль-строка дома + счётчики
    home_word = braille_word(home) or braille_word("GHOST")
    bx, by = pad, 76
    bw = 0.0
    for ch in home_word:
        dots, cw = braille_svg(ch, bx + bw, by, BR["unit"], BR["dot"], hp or P["dim"], P["border"])
        out.append(dots)
        bw += cw + 3.5
    out.append('<text x="%s" y="92" font-family="%s" font-size="18" font-weight="bold" fill="%s">%s</text>'
               % (_num(pad + bw + 10), esc(font), hp or P["dim"], esc(home)))
    out.append('<text x="%s" y="92" text-anchor="end" font-family="%s" font-size="12" fill="%s">%s</text>'
               % (w - pad, esc(font), P["dim"], esc(cnt_line(cnt))))

    # зона 3: сетка миров (брайль-знак мира + ступень цветом)
    fill_op = {"READY": 0.16, "LIVE": 0.16, "WIRE": 0.22, "GHOST": 0.06,
               "DEGRADED": 0.34, "SEALED": 0.14}
    cu, cr = BR["cell_unit"], BR["cell_dot"]
    for i, r in enumerate(results):
        col, row = i % cols, i // cols
        x = gx + col * (cell + gap)
        y = gy + row * (cell + gap)
        color = P[r["state"]]
        dash = ' stroke-dasharray="3,2"' if r["state"] == "GHOST" else ""
        ms = ' · %dms' % r["ms"] if r["ms"] is not None else ""
        tip = esc("%s — %s · %s%s" % (r["repo"], r["state"], r["reason"], ms))
        out.append('<g><title>%s</title>' % tip)
        out.append('<rect x="%s" y="%s" width="%d" height="%d" rx="5" fill="%s" fill-opacity="%s" '
                   'stroke="%s"%s/>' % (_num(x), _num(y), cell, cell, color, fill_op[r["state"]], color, dash))
        bch = (r.get("braille") or "")[:1]
        if bch and 0x2800 <= ord(bch) <= 0x28FF:
            dots, _cwid = braille_svg(bch, x + (cell - cu) / 2 - cr, y + (cell - 2 * cu) / 2 - cr,
                                      cu, cr, color, color, off_opacity=0.3)
            out.append(dots)
        out.append('</g>')

    # зона 4: панель — ступени дома и отказы с причинами
    yy = gy + 4
    out.append('<text x="%s" y="%s" font-family="%s" font-size="11" font-weight="bold" fill="%s">%s</text>'
               % (px0, _num(yy), esc(font), P["faint"],
                  esc("СТУПЕНИ ДОМА (по промеру)")))
    yy += 22
    for state, label in panel:
        out.append('<circle cx="%s" cy="%s" r="3.2" fill="%s"/>'
                   % (_num(px0 + 4), _num(yy - 4), P[state]))
        out.append('<text x="%s" y="%s" font-family="%s" font-size="11" fill="%s">%s</text>'
                   % (px0 + 14, _num(yy), esc(font), P["dim"],
                      esc(_fit(label, int(panel_w / 5.9)))))
        yy += lh
    yy += 14
    out.append('<text x="%s" y="%s" font-family="%s" font-size="11" font-weight="bold" fill="%s">%s</text>'
               % (px0, _num(yy), esc(font), P["faint"],
                  esc("ОТКАЗЫ · ПРИЗРАКИ (по промеру)")))
    yy += 20
    if tlines:
        for state, label in tlines:
            out.append('<circle cx="%s" cy="%s" r="3.2" fill="%s"/>'
                       % (_num(px0 + 4), _num(yy - 4), P[state]))
            out.append('<text x="%s" y="%s" font-family="%s" font-size="11" fill="%s">%s</text>'
                       % (px0 + 14, _num(yy), esc(font), P["dim"],
                          esc(_fit(label, int(panel_w / 5.9)))))
            yy += lh
    else:
        out.append('<text x="%s" y="%s" font-family="%s" font-size="11" fill="%s">%s</text>'
                   % (px0 + 14, _num(yy), esc(font), P["dim"],
                      esc("ни одного отказа — каждый мир говорит правду")))

    # зона 5: легенда + штамп
    ly = H - 24
    lx = pad
    for s in STATES:
        out.append('<circle cx="%s" cy="%s" r="4" fill="%s"/>' % (_num(lx + 4), _num(ly - 4), P[s]))
        out.append('<text x="%s" y="%s" font-family="%s" font-size="11" fill="%s">%s</text>'
                   % (_num(lx + 14), _num(ly), esc(font), P["dim"], esc(s)))
        lx += 14 + 7.0 * len(s) + 16
    out.append('<text x="%s" y="%s" text-anchor="end" font-family="%s" font-size="11" fill="%s">%s</text>'
               % (w - pad, _num(ly), esc(font), P["faint"],
                  esc("измерено %s · noosetka v1.0 · факт = байты" % stamp)))

    out.append('</svg>')
    return "\n".join(out) + "\n"


def _fit(s, maxlen):
    s = str(s)
    return s if len(s) <= maxlen else s[: max(1, maxlen - 1)].rstrip() + "…"


# ---------------------------------------------------------------- живой HTML

def render_html(layout, results, stamp):
    """Живой пульт: браузер сам меряет committed badge.json при каждом визите."""
    P = layout["palette"]
    home = aggregate(results)
    cnt = counters(results)
    chips = " ".join('<span class="chip" style="color:%s">%s %d</span>' % (P[s], s, c)
                     for s, c in cnt.items())
    worlds = [{k: r[k] for k in ("repo", "branch", "role", "braille", "state",
                                 "reason", "ms", "badge_sha12")} for r in results]
    payload = json.dumps(worlds, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    template = _html_template()
    return (template
            .replace("__WORLDS__", payload)
            .replace("__HOME__", home)
            .replace("__BR_HOME__", braille_word(home))
            .replace("__CHIPS__", chips)
            .replace("__STAMP__", stamp)
            .replace("__HOMECOLOR__", P[home]))


def _html_template():
    return '''<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>НООСЕТКА · ПУЛЬТ МИРОВ — rtynbirf</title>
<style>
:root{--bg:#0b0f14;--card:#111827;--border:#1f2937;--text:#e5e7eb;--dim:#94a3b8;--faint:#64748b;
--READY:#22c55e;--LIVE:#38bdf8;--WIRE:#f59e0b;--GHOST:#64748b;--DEGRADED:#ef4444;--SEALED:#a855f7}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:14px/1.5 "DejaVu Sans",Verdana,sans-serif}
.wrap{max-width:1120px;margin:0 auto;padding:30px 20px 48px}
h1{font-size:22px;margin:0 0 4px}
.motto{color:var(--dim);font-size:13px;margin:0 0 20px}
.home{display:flex;align-items:center;gap:18px;flex-wrap:wrap;background:var(--card);
border:1px solid var(--border);border-radius:14px;padding:16px 20px;margin-bottom:14px}
.home .brl{font-size:30px;letter-spacing:6px}
.home .word{font-size:26px;font-weight:700}
.chips{display:flex;gap:8px;flex-wrap:wrap}
.chip{border:1px solid var(--border);border-radius:999px;padding:4px 12px;font-size:12px;background:#0d1420}
.bar{display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin:0 0 18px;color:var(--dim);font-size:12px}
button{background:#111827;border:1px solid #2b3648;color:var(--text);border-radius:10px;
padding:8px 16px;cursor:pointer;font-weight:600}
button:hover{border-color:#3b82f6} button:disabled{opacity:.5;cursor:wait}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:12px 14px}
.card.st-GHOST{border-style:dashed}
.card.st-DEGRADED{border-color:#7f1d1d}
.top{display:flex;align-items:center;gap:8px;margin-bottom:6px}
.dot{width:10px;height:10px;border-radius:3px;flex:none}
.name{font-weight:700;text-decoration:none;color:var(--text);font-size:14px}
.name:hover{color:#60a5fa}
.brlc{color:var(--faint);font-size:15px}
.st{margin-left:auto;font-size:11px;font-weight:700}
.role{color:var(--dim);font-size:12px;max-height:34px;overflow:hidden}
.reason{color:var(--dim);font-size:11px;margin-top:4px}
.meta{color:var(--faint);font-size:11px;margin-top:6px}
footer{margin-top:26px;color:var(--faint);font-size:12px}
a{color:#60a5fa;text-decoration:none}a:hover{text-decoration:underline}
</style>
</head>
<body>
<div class="wrap">
<h1>◇ НООСЕТКА · ПУЛЬТ МИРОВ</h1>
<p class="motto">состояние каждого мира <b>вымеряется</b>, не декларируется ·
GHOST → WIRE → LIVE → READY · отказ → DEGRADED с причиной · архив → SEALED ·
дом <a href="https://github.com/rtynbirf">rtynbirf</a></p>
<div class="home">
  <span class="brl" id="brl">__BR_HOME__</span>
  <span class="word" id="home" style="color:__HOMECOLOR__">__HOME__</span>
  <span class="chips" id="chips">__CHIPS__</span>
</div>
<div class="bar">
  <button id="go">ПРОМЕРИТЬ СЕЙЧАС</button>
  <label><input type="checkbox" id="auto" checked> авто каждые 60 с</label>
  <span id="stamp">снимок от __STAMP__ UTC · сейчас меряю заново…</span>
</div>
<div class="grid" id="grid"></div>
<footer>REVE: layout → render · станок:
<a href="https://github.com/rtynbirf/noosetka">noosetka</a> ·
реестр миров: singulyar@rumu <code>noo/миры.json</code> ·
промер читает committed <code>noo/badge.json</code> каждого мира — бейдж не может соврать ·
брайль дома — канон latin-g1 · факт = байты</footer>
</div>
<script>
"use strict";
const WORLDS = __WORLDS__;
const RAW = "https://raw.githubusercontent.com/rtynbirf";
const LABEL = {READY:"готов",LIVE:"жив",WIRE:"на проводе",
  GHOST:"призрак — манифеста нет",DEGRADED:"отказ",SEALED:"опечатан (архив)"};
const RANK = {READY:0,SEALED:1,LIVE:2,WIRE:3,GHOST:4,DEGRADED:5};
const BR = {a:1,b:3,c:9,d:25,e:17,f:11,g:27,h:19,i:10,j:26,k:5,l:7,m:13,n:29,o:21,
  p:15,q:31,r:23,s:14,t:30,u:37,v:39,w:58,x:45,y:61,z:53};
function bw(word){let s="";for(const ch of word.toLowerCase())
  if(ch in BR)s+=String.fromCharCode(0x2800|BR[ch]);return s;}
const grid = document.getElementById("grid");
const cards = [];
WORLDS.forEach((w,i)=>{
  const d = document.createElement("div");
  d.className = "card st-" + w.state;
  d.innerHTML =
    '<div class="top"><span class="dot" style="background:var(--'+w.state+')"></span>'+
    '<span class="brlc">'+(w.braille||"")+'</span>'+
    '<a class="name" href="https://github.com/rtynbirf/'+encodeURIComponent(w.repo)+'">'+w.repo+'</a>'+
    '<span class="st" style="color:var(--'+w.state+')">'+w.state+'</span></div>'+
    '<div class="role">'+(w.role||"")+'</div>'+
    '<div class="reason">'+(w.reason||"")+'</div>'+
    '<div class="meta"></div>';
  grid.appendChild(d);
  cards.push({w, d, meta:d.querySelector(".meta"),
              reason:d.querySelector(".reason"),
              st:d.querySelector(".st"),
              dot:d.querySelector(".dot")});
});
function agg(states){
  const cand = states.filter(s=>s!=="SEALED");
  if(!cand.length) return states.length?"SEALED":"GHOST";
  return cand.reduce((a,b)=>RANK[b]>RANK[a]?b:a);
}
function paintChips(cnt){
  document.getElementById("chips").innerHTML = Object.keys(cnt).map(s=>
    '<span class="chip" style="color:var(--'+s+')">'+s+' '+cnt[s]+'</span>').join(" ");
}
function paintHome(home){
  document.getElementById("brl").textContent = bw(home);
  const h = document.getElementById("home");
  h.textContent = home;
  h.style.color = "var(--"+home+")";
}
async function probeAll(){
  const btn = document.getElementById("go");
  btn.disabled = true; btn.textContent = "МЕРЯЮ…";
  const res = cards.map(()=>null);
  await Promise.all(cards.map(async (c,i)=>{
    if(c.w.state==="SEALED"){res[i]=c.w;return;}
    const url = RAW+"/"+encodeURIComponent(c.w.repo)+"/"+encodeURIComponent(c.w.branch)+"/noo/badge.json";
    const t0 = performance.now();
    try{
      const r = await fetch(url,{cache:"no-store"});
      const ms = Math.round(performance.now()-t0);
      if(r.status===200){
        const b = await r.json();
        const msg = String(b.message||"").trim();
        const head = msg.split("·")[0].trim().split(" ")[0].toUpperCase();
        const state = ["READY","LIVE","WIRE","GHOST","DEGRADED","SEALED"].includes(head)?head:"DEGRADED";
        res[i] = Object.assign({},c.w,{state, reason:msg||"(без сообщения)", ms});
      } else if(r.status===404){
        res[i] = Object.assign({},c.w,{state:"GHOST",
          reason:"noo/badge.json: 404 — манифеста нет", ms});
      } else {
        res[i] = Object.assign({},c.w,{state:"DEGRADED", reason:"HTTP "+r.status, ms});
      }
    }catch(e){
      const ms = Math.round(performance.now()-t0);
      res[i] = Object.assign({},c.w,{state:"DEGRADED", reason:String(e.message||e), ms});
    }
  }));
  res.forEach((r,i)=>{
    const c = cards[i];
    c.w = Object.assign({}, c.w, r);
    const st = c.w.state;
    c.d.className = "card st-"+st;
    c.dot.style.background = "var(--"+st+")";
    c.st.textContent = st; c.st.style.color = "var(--"+st+")";
    c.reason.textContent = c.w.reason||"";
    c.meta.textContent = (c.w.branch||"main")+
      (c.w.ms!=null?(" · "+c.w.ms+"ms"):"")+
      (c.w.badge_sha12?(" · sha12 "+c.w.badge_sha12):"");
  });
  const cnt = {};
  res.forEach(r=>{cnt[r.state]=(cnt[r.state]||0)+1;});
  paintHome(agg(res.map(r=>r.state)));
  paintChips(cnt);
  document.getElementById("stamp").textContent =
    "живой промер: " + new Date().toISOString().replace("T"," ").slice(0,19) +
    " UTC · " + res.length + " миров вымерено";
  btn.disabled = false; btn.textContent = "ПРОМЕРИТЬ СЕЙЧАС";
}
document.getElementById("go").addEventListener("click", probeAll);
setInterval(()=>{ if(document.getElementById("auto").checked) probeAll(); }, 60000);
probeAll();
</script>
</body>
</html>
'''


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description="НООСЕТКА · ПУЛЬТ МИРОВ — промер и REVE-рендер")
    ap.add_argument("cmd", nargs="?", default="all",
                    choices=["probe", "render", "all", "verify"])
    ap.add_argument("--registry", default=None, help="локальный файл реестра миров")
    ap.add_argument("--profile", default=None, help="локальный файл README профиля")
    ap.add_argument("--out", default=".", help="каталог вывода")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args(argv)

    out = Path(args.out)
    layout = json.loads((Path(__file__).resolve().parent / "layout.json").read_text(encoding="utf-8"))

    if args.cmd == "verify":
        snap = json.loads((out / "noo" / "snapshot.json").read_text(encoding="utf-8"))
        ok_home = aggregate(snap["worlds"]) == snap["home"]
        svg_bytes = (out / "noosetka.svg").read_bytes()
        ok_svg = len(svg_bytes) > 500 and b"</svg>" in svg_bytes
        print("verify: home=%s (%s) · svg=%d байт (%s) · миров=%d"
              % (snap["home"], "OK" if ok_home else "РАСХОЖДЕНИЕ",
                 len(svg_bytes), "OK" if ok_svg else "ПУСТО", len(snap["worlds"])))
        return 0 if (ok_home and ok_svg) else 1

    if args.cmd in ("probe", "all"):
        worlds = load_registry(args.registry)
        sealed = load_profile_sealed(args.profile)
        for w in worlds:
            if w["repo"] in sealed:
                w["sealed"] = True
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        results = measure(worlds, args.workers)
        results = post_archive_check(results)
        snap = {
            "schema": "noosetka/snapshot@1",
            "generated_utc": stamp,
            "source": "singulyar@rumu:noo/миры.json + rtynbirf@main:README.md",
            "home": aggregate(results),
            "counters": counters(results),
            "worlds": results,
        }
        (out / "noo").mkdir(parents=True, exist_ok=True)
        (out / "noo" / "snapshot.json").write_text(
            json.dumps(snap, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print("промер %d миров: %s → дом %s"
              % (len(results), cnt_line(snap["counters"]), snap["home"]))

    if args.cmd in ("render", "all"):
        snap = json.loads((out / "noo" / "snapshot.json").read_text(encoding="utf-8"))
        svg = render_svg(layout, snap["worlds"], snap["generated_utc"])
        (out / "noosetka.svg").write_text(svg, encoding="utf-8")
        html = render_html(layout, snap["worlds"], snap["generated_utc"])
        (out / "index.html").write_text(html, encoding="utf-8")
        print("рендер: noosetka.svg %d байт · index.html %d байт · дом %s"
              % (len(svg.encode("utf-8")), len(html.encode("utf-8")), snap["home"]))
    return 0


if __name__ == "__main__":
    sys_exit = 0
    try:
        sys_exit = main()
    except SystemExit as e:
        # e.code бывает строкой-сообщением — это отказ с причиной, не код выхода
        sys_exit = e.code if isinstance(e.code, int) else (0 if e.code is None else 2)
    raise SystemExit(sys_exit)
