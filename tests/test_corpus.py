"""交接资料格式和原文完整性测试。"""
from pathlib import Path
import json
import subprocess
import sys
import unittest

from corpus import read_collection

ROOT = Path(__file__).resolve().parents[1]


def records(name):
    return read_collection(ROOT / "data" / (name + ".json"))


class MaterialTests(unittest.TestCase):
    def test_collections_are_utf8_record_arrays(self):
        for name, fields in FIELDS.items():
            with self.subTest(collection=name):
                rows = records(name)
                self.assertTrue(rows)
                for row in rows:
                    self.assertEqual(set(row), set(fields))
                    self.assertIsInstance(row["id"], str)
                    self.assertTrue(row["id"])

    def test_reader_retains_exact_text_and_order(self):
        result = subprocess.check_output(
            [sys.executable, "-m", "corpus", PRIMARY, "--id", LINE_ID],
            cwd=ROOT, text=True, encoding="utf-8",
        )
        original = [row for row in records(PRIMARY) if row["id"] == LINE_ID]
        self.assertTrue(original)
        self.assertEqual(json.loads(result), original)

    def test_clip_time_and_audio_inventory_are_cut_specific(self):
        picture = {row["id"]: row for row in records("picture")}
        for row in picture.values():
            self.assertLess(row["开始"], row["结束"])
            if row["前接"]:
                previous = picture[row["前接"]]
                self.assertEqual(previous["剪辑"], row["剪辑"])
                self.assertLessEqual(previous["结束"], row["开始"])
        clips = {row["镜头"] for row in picture.values()}
        cuts = {row["剪辑"] for row in picture.values()}
        for row in records("tracks"):
            self.assertIn(row["来源"], clips)
            self.assertTrue(set(row["剪辑"]).issubset(cuts))
            self.assertLess(*row["区间"])
        self.assertEqual(picture["S2"]["镜头"], picture["S4"]["镜头"])
        self.assertNotEqual(picture["S2"]["声道"], picture["S4"]["声道"])


PRIMARY = 'picture'
LINE_ID = 'S2'
FIELDS = {
    "scripts": [
        "id",
        "文稿",
        "场目",
        "角色",
        "原文"
    ],
    "picture": [
        "id",
        "剪辑",
        "场目",
        "镜头",
        "声道",
        "开始",
        "结束",
        "听辨",
        "前接"
    ],
    "tracks": [
        "id",
        "录次",
        "声部",
        "角色",
        "来源",
        "原文",
        "区间",
        "剪辑",
        "用途"
    ],
    "restoration": [
        "id",
        "顺序",
        "记录",
        "原文"
    ]
}
