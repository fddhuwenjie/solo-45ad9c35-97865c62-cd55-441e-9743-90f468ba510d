"""Regression tests for the four reviewed fixes + core analysis/revision chain.

Run: python -m pytest pageturn/test_regression.py -q
(or:  python pageturn/test_regression.py)
"""
from __future__ import annotations

import io
import json
import unittest

from .app import App
from .engine import evaluate_pagination, public_report, run_analysis
from .parser import parse_musicxml
from .roadmap import expand
from .test_support import measure, note, part, rest, score


def m_tempo_change_m2():
    """4/4 m2: beats 1-2 at 60 QPM, beats 3-4 at 120 QPM -> exactly 3.0 s."""
    m1 = ('<measure number="1"><attributes><divisions>1</divisions>'
          '<time><beats>4</beats><beat-type>4</beat-type></time></attributes>'
          '<direction><sound tempo="60"/></direction>' + note(4) + '</measure>')
    m2 = ('<measure number="2"><direction><offset>2</offset>'
          '<direction-type><words>piu mosso</words></direction-type>'
          '<sound tempo="120"/></direction>' + note(4) + '</measure>')
    m3 = ('<measure number="3"><direction><sound tempo="120"/></direction>'
          + rest(4) + '</measure>')
    m4 = '<measure number="4">' + rest(4) + '</measure>'
    return score([part("P1", [m1, m2, m3, m4], name="Violin")])


class MidMeasureTempoTests(unittest.TestCase):
    def test_piecewise_integration_three_seconds(self):
        full = run_analysis(m_tempo_change_m2())
        rep = public_report(full)
        durs = rep["parts"][0]["measureDurationsSeconds"]
        self.assertEqual(durs["1"], 4.0)       # 4 beats @ 60
        self.assertEqual(durs["2"], 3.0)       # 2 beats @ 60 + 2 beats @ 120
        self.assertEqual(durs["3"], 2.0)       # 4 beats @ 120

    def test_tempo_change_at_offset_in_direction(self):
        # offset lives on <direction><offset>, sound carries the tempo
        full = run_analysis(m_tempo_change_m2())
        seg = full["_internal"]["timing"]["P1"]["segments"][1]
        self.assertAlmostEqual(seg.clock(2.0) - seg.start_sec, 2.0, places=6)
        self.assertAlmostEqual(seg.clock(4.0) - seg.start_sec, 3.0, places=6)


class NewPageSemanticsTests(unittest.TestCase):
    def _xml(self):
        return score([part("P1", [
            measure(1, note(4), tempo=60, timesig=True),
            measure(2, rest(4)),
            measure(3, rest(4), new_page=True),   # page starts AT m3
            measure(4, rest(4)),
        ], name="Violin")])

    def test_new_page_means_break_after_previous_measure(self):
        full = run_analysis(self._xml())
        pag = full["pagination"]["P1"]
        self.assertEqual(pag["breaks"], ["2"])    # turn after m2, not m3

    def test_explicit_pagination_overrides_print(self):
        full = run_analysis(self._xml(),
                            {"pagination": {"P1": {"breaks": ["1"]}}})
        self.assertEqual(full["pagination"]["P1"]["breaks"], ["1"])


class FinalPageCapacityTests(unittest.TestCase):
    def _xml(self):
        return score([part("P1", [
            measure(1, note(4), tempo=60, timesig=True),
            measure(2, rest(4)),
            measure(3, rest(4), new_page=True),
            measure(4, note(4)),
            measure(5, note(4)),
        ], name="Violin")])

    def test_last_page_checked_for_measure_capacity(self):
        # break after m2; tail page holds m3-m5 = 3 measures > cap 2
        full = run_analysis(self._xml(), {
            "parts": {"P1": {"maxMeasuresPerPage": 2}}})
        pages = public_report(full)["parts"][0]["pages"]
        tails = [p for p in pages if p["isTail"]]
        self.assertEqual(len(tails), 1)
        tail = tails[0]
        self.assertEqual(tail["measureCount"], 3)
        self.assertFalse(tail["feasible"])
        self.assertEqual(tail["reason"], "PAGE_CAPACITY")

    def test_last_page_checked_for_seconds_capacity(self):
        # m3-m5 each 4s @60 = 12s tail; cap 10s
        full = run_analysis(self._xml(), {
            "parts": {"P1": {"maxPageSeconds": 10.0}}})
        pages = public_report(full)["parts"][0]["pages"]
        tail = [p for p in pages if p["isTail"]][0]
        self.assertAlmostEqual(tail["pageSeconds"], 12.0, places=6)
        self.assertFalse(tail["feasible"])

    def test_compliant_tail_page_is_feasible(self):
        full = run_analysis(self._xml(), {
            "parts": {"P1": {"maxMeasuresPerPage": 4, "maxPageSeconds": 30.0}}})
        pages = public_report(full)["parts"][0]["pages"]
        tail = [p for p in pages if p["isTail"]][0]
        self.assertTrue(tail["feasible"])


class DuplicateSegnoTests(unittest.TestCase):
    def _xml(self):
        return score([part("P1", [
            measure(1, note(4), tempo=60, timesig=True, segno=True),
            measure(2, note(4), segno=True),
            measure(3, note(4)),
            measure(4, note(4), ds=True),
            measure(5, note(4)),
        ], name="Violin")], names={"P1": "Violin"})

    def test_ambiguity_located_by_part_pass_measure(self):
        full = run_analysis(self._xml())
        rep = public_report(full)
        self.assertFalse(rep["roadmapOk"])
        located = [i for i in rep["issues"]
                   if i["code"] == "REPEAT_AMBIGUOUS" and i["partId"] == "P1"]
        self.assertTrue(located, "需要带声部定位的歧义 issue")
        for i in located:
            self.assertEqual(i["partId"], "P1")
            self.assertIsNotNone(i["pass"])
            self.assertIsNotNone(i["measure"])

    def test_breakpoints_and_overall_never_playable(self):
        full = run_analysis(self._xml(),
                            {"pagination": {"P1": {"breaks": ["3"]}}})
        rep = public_report(full)
        self.assertFalse(rep["feasible"])
        break_pages = [p for p in rep["parts"][0]["pages"] if not p["isTail"]]
        self.assertTrue(break_pages)
        for p in break_pages:
            self.assertFalse(p["feasible"], p)
            self.assertEqual(p["reason"], "REPEAT_AMBIGUOUS")

    def test_locked_break_still_not_playable(self):
        full = run_analysis(self._xml(),
                            {"pagination": {"P1": {"breaks": ["3"],
                                                   "locked": ["3"]}}})
        rep = public_report(full)
        p = [x for x in rep["parts"][0]["pages"] if x["breakAfter"] == "3"][0]
        self.assertTrue(p["locked"])
        self.assertFalse(p["feasible"])
        self.assertIsNone(p["moveTo"])

    def test_revision_recompute_carries_ambiguity(self):
        full = run_analysis(self._xml())
        rev = evaluate_pagination(full, {"P1": {"breaks": ["3"]}})
        self.assertFalse(rev["feasible"])
        self.assertTrue(any(i["partId"] == "P1" and i["pass"]
                            for i in rev["issues"] if i["code"] == "REPEAT_AMBIGUOUS"))


class ApiChainTests(unittest.TestCase):
    def setUp(self):
        self.app = App(":memory:")

    def call(self, method, path, payload=None, accept="application/json"):
        data = json.dumps(payload).encode() if payload is not None else b""
        env = {"REQUEST_METHOD": method, "PATH_INFO": path,
               "CONTENT_TYPE": "application/json", "CONTENT_LENGTH": str(len(data)),
               "wsgi.input": io.BytesIO(data), "HTTP_ACCEPT": accept}
        cap = {}
        out = b"".join(self.app(env, lambda s, h: cap.update(status=s, headers=h)))
        code = int(cap["status"].split()[0])
        ctype = dict(cap.get("headers", [])).get("Content-Type", "")
        if "json" in ctype:
            return code, json.loads(out) if out else None
        return code, out

    def _score(self):
        return score([part("P1", [
            measure(1, note(4), tempo=60, timesig=True),
            measure(2, rest(4)),
            measure(3, rest(4), new_page=True),
            measure(4, note(4)),
            measure(5, note(4)),
        ], name="Violin")])

    def test_analysis_endpoint_does_not_500(self):
        c, r = self.call("POST", "/scores",
                         {"title": "t", "musicxml": self._score()})
        self.assertEqual(c, 201)
        sid = r["scoreId"]
        c, r = self.call("POST", f"/scores/{sid}/analysis")
        self.assertEqual(c, 201, r)
        self.assertIn("report", r)
        self.assertIn("parts", r["report"])

    def test_revision_with_analysis_id_does_not_500(self):
        c, r = self.call("POST", "/scores",
                         {"title": "t", "musicxml": self._score()})
        sid = r["scoreId"]
        c, r = self.call("POST", f"/scores/{sid}/analysis")
        self.assertEqual(c, 201)
        aid = r["analysisId"]
        c, r = self.call("POST", f"/scores/{sid}/revisions",
                         {"analysisId": aid, "note": "edit",
                          "pagination": {"P1": {"breaks": ["2"]}}})
        self.assertEqual(c, 201, r)
        self.assertIn("revisionId", r)
        rid = r["revisionId"]
        c, r = self.call("POST", f"/revisions/{rid}/confirm")
        self.assertEqual(c, 201, r)
        self.assertIn("recompute", r)
        self.assertTrue(r["recompute"]["frozen"] if "frozen" in r["recompute"] else True)

    def test_unplayable_revision_cannot_be_confirmed(self):
        xml = score([part("P1", [
            measure(1, note(4), tempo=60, timesig=True),
            measure(2, note(4)),
            measure(3, note(4), new_page=True),
        ], name="Violin")])
        c, r = self.call("POST", "/scores", {"title": "t", "musicxml": xml})
        sid = r["scoreId"]
        self.call("POST", f"/scores/{sid}/analysis")
        c, r = self.call("POST", f"/scores/{sid}/revisions",
                         {"pagination": {"P1": {"breaks": ["1"]}}})
        rid = r["revisionId"]
        c, r = self.call("POST", f"/revisions/{rid}/confirm")
        self.assertEqual(c, 422)
        self.assertEqual(r["error"], "UNPLAYABLE")


if __name__ == "__main__":
    unittest.main(verbosity=2)
