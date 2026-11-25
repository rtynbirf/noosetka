#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тесты НООСЕТКИ: брайль-канон, агрегат дома, REVE-детерминизм, честный промер."""
import json
import unittest
from pathlib import Path

import noosetka as N

STAMP = "2026-01-01T00:00:00Z"


def world(repo, state, **kw):
    base = {"repo": repo, "branch": "main", "role": "тест-мир " + repo,
            "braille": kw.get("braille", ""), "sealed": False,
            "state": state, "reason": state + " · тест", "ms": 42, "badge_sha12": "abc123def456"}
    base.update({k: v for k, v in kw.items() if k in base or k in ("sealed",)})
    return base


FIXTURE = [
    world("alpha", "READY", braille="⠗"),
    world("beta", "LIVE", braille="⠑"),
    world("gamma", "WIRE", braille="⠉"),
    world("delta", "GHOST", braille="⠙"),
    world("epsilon", "DEGRADED", braille="⠑"),
    world("zeta", "SEALED", braille="⠵"),
]
LAYOUT = json.loads((Path(__file__).resolve().parent / "layout.json").read_text(encoding="utf-8"))


class BrailleCanon(unittest.TestCase):
    """Брайль-строка дома — канон latin-g1, байты профиля не врут."""

    def test_ready(self):
        self.assertEqual(N.braille_word("READY"), "⠗⠑⠁⠙⠽")

    def test_degraded(self):
        self.assertEqual(N.braille_word("DEGRADED"), "⠙⠑⠛⠗⠁⠙⠑⠙")

    def test_bits_r(self):
        # r = точки {1,2,3,5}
        self.assertEqual(N.braille_bits("⠗"), [1, 1, 1, 0, 1, 0])


class AggregateHouse(unittest.TestCase):
    """Дом = худшая ступень среди живых; SEALED не роняет; DEGRADED главнее всех."""

    def test_ready_with_sealed(self):
        self.assertEqual(N.aggregate([FIXTURE[0], FIXTURE[5]]), "READY")

    def test_degraded_dominates(self):
        self.assertEqual(N.aggregate([FIXTURE[0], FIXTURE[4]]), "DEGRADED")

    def test_ghost_worse_than_wire(self):
        self.assertEqual(N.aggregate([FIXTURE[0], FIXTURE[3]]), "GHOST")
        self.assertEqual(N.aggregate([FIXTURE[0], FIXTURE[2]]), "WIRE")

    def test_all_sealed(self):
        self.assertEqual(N.aggregate([FIXTURE[5]]), "SEALED")

    def test_counters(self):
        c = N.counters(FIXTURE)
        self.assertEqual(c, {"READY": 1, "LIVE": 1, "WIRE": 1,
                             "GHOST": 1, "DEGRADED": 1, "SEALED": 1})


class RevealRender(unittest.TestCase):
    """REVE: layout → render. Один и тот же layout даёт одинаковые байты."""

    def test_svg_determinism(self):
        a = N.render_svg(LAYOUT, FIXTURE, STAMP)
        b = N.render_svg(LAYOUT, FIXTURE, STAMP)
        self.assertEqual(a.encode("utf-8"), b.encode("utf-8"))

    def test_html_determinism(self):
        a = N.render_html(LAYOUT, FIXTURE, STAMP)
        b = N.render_html(LAYOUT, FIXTURE, STAMP)
        self.assertEqual(a.encode("utf-8"), b.encode("utf-8"))

    def test_svg_carries_facts(self):
        svg = N.render_svg(LAYOUT, FIXTURE, STAMP)
        for needle in ("НООСЕТКА · ПУЛЬТ МИРОВ", "alpha", "delta",
                       "GHOST", "DEGRADED", "SEALED", STAMP):
            self.assertIn(needle, svg, "пульт молчит о %r" % needle)

    def test_svg_braille_is_dots_not_font(self):
        """Брайль рисуется точками: у дома 8×6 + у 6 миров 6×6 + легенда ≥ 90 кругов."""
        svg = N.render_svg(LAYOUT, FIXTURE, STAMP)
        self.assertGreaterEqual(svg.count("<circle"), 90)

    def test_html_carries_worlds(self):
        html = N.render_html(LAYOUT, FIXTURE, STAMP)
        for needle in ("alpha", "epsilon", "ПРОМЕРИТЬ СЕЙЧАС", "noo/badge.json",
                       N.braille_word(N.aggregate(FIXTURE))):
            self.assertIn(needle, html)
        self.assertNotIn("</script><script", html)  # без XSS-щелей


class HonestProbe(unittest.TestCase):
    """Пример честного промера: недоступен — так и говорим, с причиной."""

    def test_dead_endpoint_is_degraded(self):
        r = N.probe({"repo": "x", "branch": "main", "role": "", "braille": "",
                     "sealed": False}, base="http://127.0.0.1:1")
        self.assertEqual(r["state"], "DEGRADED")
        self.assertTrue(r["reason"], "причина обязана быть")

    def test_sealed_never_probed(self):
        r = N.probe({"repo": "arch", "branch": "main", "role": "", "braille": "",
                     "sealed": True}, base="http://127.0.0.1:1")
        self.assertEqual(r["state"], "SEALED")
        self.assertIn("архив", r["reason"])


class Sources(unittest.TestCase):
    """Реестр миров и профиль — источники байт."""

    def test_registry_fixture(self):
        tmp = Path("/tmp/test_miry.json")
        tmp.write_text(json.dumps({
            "schema": "x",
            "миры": [
                {"репо": "b", "роль": "второй", "ветка": "main", "порядок": 2},
                {"репо": "a", "роль": "первый", "ветка": "rumu", "порядок": 1},
            ]}, ensure_ascii=False), encoding="utf-8")
        worlds = N.load_registry(str(tmp))
        self.assertEqual([w["repo"] for w in worlds], ["a", "b"])
        self.assertEqual(worlds[0]["branch"], "rumu")

    def test_profile_sealed_marker(self):
        text = (
            "| [`live`](https://github.com/rtynbirf/live) | "
            "![НОО live](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2Frtynbirf%2Flive%2Fmain%2Fnoo%2Fbadge.json) | жив |\n"
            "| [`universal-agent`](https://github.com/rtynbirf/universal-agent) | "
            "`SEALED` · архив (заморожен владельцем, запись в архив закрыта GitHub) | TsUA |\n"
        )
        self.assertEqual(N.parse_profile_sealed(text), {"universal-agent"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
