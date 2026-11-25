"""缺音重建与补录交接的业务规则测试。"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from corpus import read_collection
import restore as r


def rows(name):
    return read_collection(ROOT / "data" / (name + ".json"))


class HearingTests(unittest.TestCase):
    def test_full_gap_is_distinct_from_partial_gap(self):
        self.assertTrue(r.is_full_gap("〔全段缺音〕"))
        # 全段缺音不是可填词的半句缺口：没有任何可辨原字，也不产生普通缺口片段
        self.assertEqual(r.audible_parts("〔全段缺音〕"), "")
        self.assertEqual([p for p in r.split_hearing("别告诉〔缺音〕。") if p[1]],
                         [("〔缺音〕", True)])

    def test_audible_parts_keep_original_characters_and_drop_notes(self):
        hearing = "别告诉〔缺音〕进城了。场记暂登记同期，声源有疑。"
        self.assertEqual(r.audible_parts(hearing), "别告诉进城了。")
        self.assertTrue(r.has_source_doubt(hearing))
        self.assertEqual(r.note_text(hearing), "场记暂登记同期，声源有疑。")

    def test_strict_fit_only_allows_extra_words_inside_gap(self):
        # K4 与 S2 两侧原字连续，缺口中文字可定位
        ok, fill = r.fits_prefix_suffix("别告诉那个人我们进城了。",
                                        "别告诉〔缺音〕进城了。场记暂登记同期，声源有疑。")
        self.assertTrue(ok)
        self.assertEqual(fill, "那个人我们")
        # K6 的“他”落在可辨句号之前而不是缺音位置，严格匹配失败、宽松匹配成立
        strict, _ = r.fits_prefix_suffix("别告诉他。等舅舅回来。", "别告诉〔缺音〕。")
        loose, loose_fill = r.fits_prefix_suffix(
            "别告诉他。等舅舅回来。", "别告诉〔缺音〕。", strict=False)
        self.assertFalse(strict)
        self.assertTrue(loose)
        self.assertEqual(loose_fill, "他等舅舅回来。")


class ScriptFactTests(unittest.TestCase):
    def setUp(self):
        self.facts = r.script_facts(rows("scripts"))

    def test_crossed_out_and_deleted_lines_are_extracted_with_sources(self):
        self.assertEqual(self.facts["划掉未录"], [{"出处": "P5", "原文": "船后还有一个人"}])
        self.assertEqual(self.facts["正片删除"], [{"出处": "P7", "原文": "等船靠稳再说"}])
        self.assertEqual(self.facts["仅环境声镜头"], {"B20"})
        self.assertEqual(self.facts["环境声出处"]["B20"], "P7")

    def test_offscreen_retell_is_scoped_by_cut_version(self):
        scripts = rows("scripts")
        self.assertEqual(r.offscreen_retell_shots(self.facts, scripts, "院线"), {"B19"})
        # 靠岸版 B19 的旁白来自其自身旁白行，P7 的画外安排不跨版本生效
        self.assertNotIn("B19", r.offscreen_retell_shots(self.facts, scripts, "靠岸"))


class AssessmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.picture = rows("picture")
        cls.tracks = rows("tracks")
        cls.scripts = rows("scripts")
        cls.restoration = rows("restoration")
        cls.assessments, cls.facts = r.assess_picture(
            cls.picture, cls.tracks, cls.scripts, cls.restoration)
        cls.by_id = {a["成片行"]: a for a in cls.assessments}

    def test_intact_lines_are_anchors(self):
        for sid in ("S1", "S7"):
            self.assertEqual(self.by_id[sid]["决定"], r.INTACT)
            self.assertEqual(self.by_id[sid]["保留原字"], self.by_id[sid]["听辨原文"])

    def test_s2_uses_approved_narration_but_requires_verification(self):
        s2 = self.by_id["S2"]
        self.assertEqual(s2["决定"], r.VERIFY)
        self.assertEqual(s2["选定音轨"], "K4")
        self.assertEqual(s2["说话人"], "梅姨")
        self.assertTrue(s2["声源有疑"])
        k2 = next(c for c in s2["候选"] if c["音轨"] == "K2")
        self.assertFalse(k2["成立"])
        self.assertTrue(any("正片删除" in reason for reason in k2["不成立原因"]))
        # 原声听出的字不能被台本相似句覆盖
        self.assertEqual(s2["保留原字"], "别告诉进城了。")

    def test_s3_full_gap_is_kept_not_filled(self):
        s3 = self.by_id["S3"]
        self.assertEqual(s3["决定"], r.KEEP)
        self.assertNotIn("选定音轨", s3)
        self.assertTrue(any("P7" in basis for basis in s3["依据"]))

    def test_s4_borrows_narration_track(self):
        s4 = self.by_id["S4"]
        self.assertEqual(s4["决定"], r.BORROW)
        self.assertEqual(s4["选定音轨"], "K3")
        self.assertEqual(s4["缺口填充文字"], "到了再告诉她")
        self.assertEqual(s4["说话人"], "梅姨")

    def test_s5_borrows_k1_with_install_record_bounded_to_that_cut(self):
        s5 = self.by_id["S5"]
        self.assertEqual(s5["决定"], r.BORROW)
        self.assertEqual(s5["选定音轨"], "K1")
        self.assertEqual(s5["缺口填充文字"], "了再告")
        self.assertTrue(any("R7" in basis for basis in s5["依据"]))

    def test_s6_keeps_comparison_track_and_does_not_split_breath(self):
        s6 = self.by_id["S6"]
        self.assertEqual(s6["决定"], r.VERIFY)
        self.assertNotIn("选定音轨", s6)
        k6 = next(c for c in s6["候选"] if c["音轨"] == "K6")
        self.assertFalse(k6["成立"])
        self.assertTrue(k6["留作对照"])
        self.assertEqual(s6["对照音轨"], "K6")

    def test_k2_window_limit_blocks_half_breath_borrowing(self):
        s6 = self.by_id["S6"]
        k2 = next(c for c in s6["候选"] if c["音轨"] == "K2")
        self.assertTrue(any("半句" in reason for reason in k2["不成立原因"]))

    def test_every_rejected_candidate_states_reasons(self):
        for entry in self.assessments:
            for candidate in entry["候选"]:
                if not candidate["成立"]:
                    self.assertTrue(candidate["不成立原因"])


class ScheduleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        picture = rows("picture")
        tracks = rows("tracks")
        scripts = rows("scripts")
        restoration = rows("restoration")
        assessments, _ = r.assess_picture(picture, tracks, scripts, restoration)
        cls.orders, cls.logs, cls.bookings = r.reconcile_schedule(
            assessments, restoration, tracks)
        cls.by_sid = {}
        for order in cls.orders:
            cls.by_sid.setdefault(order["成片行"], order)

    def test_two_bookings_are_parsed_separately_by_sentence(self):
        self.assertEqual([b["成片行"] for b in self.bookings], [["S2", "S3"], ["S6"]])
        self.assertEqual([b["演员"] for b in self.bookings], ["陆", "少年"])

    def test_s3_booking_is_cancelled_after_supplementary_page(self):
        self.assertEqual(self.by_sid["S3"]["处置"], "撤销补录")

    def test_s2_pending_verification_can_be_re_determined(self):
        order = self.by_sid["S2"]
        self.assertEqual(order["处置"], "核实后定")
        self.assertIn("K4", order["说明"])
        self.assertIn("梅姨", order["说明"])

    def test_recorded_s6_is_retained_with_actual_reading(self):
        order = self.by_sid["S6"]
        self.assertEqual(order["处置"], "已录待核")
        self.assertIn("别告诉他。等舅舅回来。", order["说明"])
        self.assertTrue(any(log["分类"] == r.LOG_RETAIN for log in self.logs))


class LedgerTests(unittest.TestCase):
    def test_install_ledger_bounds_confirmation_to_checked_cut_and_shot(self):
        handoff = r.build_handoff(ROOT / "data")
        ledger = handoff["采用台账"]
        self.assertEqual(len(ledger), 1)
        item = ledger[0]
        self.assertEqual((item["成片行"], item["采用音轨"], item["剪辑"], item["镜头"]),
                         ("S5", "K1", "靠岸", "B18"))
        self.assertIn("不得据此默认适用", item["适用限制"])


class CliTests(unittest.TestCase):
    def test_json_output_is_valid_and_contains_all_picture_rows(self):
        output = subprocess.check_output(
            [sys.executable, "-I", str(ROOT / "restore.py"), "--json"],
            text=True, encoding="utf-8")
        payload = json.loads(output)
        ids = {entry["成片行"] for entry in payload["评估"]}
        self.assertEqual(ids, {f"S{i}" for i in range(1, 8)})
        summary = payload["交接清单"]
        self.assertEqual(summary["可直接借用的原录音"], ["S4", "S5"])
        self.assertEqual(summary["须先核实或原样保留的位置"], ["S2", "S3", "S6"])
        self.assertEqual(summary["原声完整锚点"], ["S1", "S7"])

    def test_report_runs_under_isolated_mode(self):
        output = subprocess.check_output(
            [sys.executable, "-I", str(ROOT / "restore.py")],
            text=True, encoding="utf-8")
        self.assertIn("电影修复缺音台词交接清单", output)


if __name__ == "__main__":
    unittest.main()
