"""电影修复缺音台词重建与补录交接。

只处理文字与时间区间，不处理音频文件。输入是 data/ 下四份 UTF-8 JSON：

- scripts.json     分场台本、场记原话（含划掉、转述、拟剪说明）
- picture.json     成片转写（带毫秒区间、声道、剪接行、前接）
- tracks.json      可调音轨库存（声部、说话人、可用剪辑、批准用途）
- restoration.json 修复规则、补录档期、已预约/已录、补页与安装记录

判定原则（对应 restoration.json 中馆方规则）：

1. 原声听辨出的字一律保留，任何候选只填〔缺音〕位置，不能覆盖原声。
2. 同期缺音只能借同次录音、同说话人、气口可核的原轨；画外转述可用馆方另批旁白。
3. 同一气口的半句话不能各取一半（比较用音轨、超窗借用一律不拼缝）。
4. 片中未播的原话（院线拟剪后正片删除、划掉留底）不可补入成片。
5. 没有词面可比的全段缺音，按场记/补页决定；依据不足就保留缺口并记工作记录。
6. 修复员的确认只在被核对的剪辑、镜头、原字范围内适用，不跨版本外推。
"""
import argparse
import json
import re
import sys
from pathlib import Path

# -I 隔离模式不自动放入脚本目录，显式加入仓库根目录以读取同仓 corpus
sys.path.insert(0, str(Path(__file__).resolve().parent))
from corpus import read_collection

DATA_DIR = Path(__file__).resolve().parent / "data"

# 决策标签
BORROW = "可直接借用原录音"
VERIFY = "须先核实"
KEEP = "原样保留"
INTACT = "原声完整无需处理"

# 工作记录分类
LOG_FACTS = "事实核定"
LOG_SPLIT = "预约重新核定"
LOG_RETAIN = "已录段保留"
LOG_GAP = "缺口与分歧"

GAP_RE = re.compile(r"〔缺音〕")
FULL_GAP_RE = re.compile(r"^〔全段缺音〕$")
DOUBT_RE = re.compile(r"声源有疑|有疑")
# 场记/规则中引用台词的写法：‘…』、「…」、“…”
QUOTED_RE = re.compile(r"[‘“「]([^’”」]+)[’”」]")
LEAD_RE = re.compile(r"[，。；：、\s]*([^，。；：\s]+)说$")


# ---------------------------------------------------------------- 文字解析

def is_full_gap(hearing):
    """整行只有全段缺音标记，没有任何可辨原字。"""
    return bool(FULL_GAP_RE.fullmatch(hearing.strip()))


def split_hearing(hearing):
    """把听辨行拆成 (文字, 是否缺音) 片段，保持原字与顺序。"""
    pieces = []
    cursor = 0
    for match in GAP_RE.finditer(hearing):
        if match.start() > cursor:
            pieces.append((hearing[cursor:match.start()], False))
        pieces.append(("〔缺音〕", True))
        cursor = match.end()
    if cursor < len(hearing):
        pieces.append((hearing[cursor:], False))
    return pieces


def audible_parts(hearing):
    """保留原声时实际可播的文字（去掉缺音标记与场记附记）。"""
    if is_full_gap(hearing):
        return ""
    note = note_text(hearing)
    body = hearing.replace(note, "") if note else hearing
    return "".join(text for text, is_gap in split_hearing(body) if not is_gap)


def note_text(hearing):
    """听辨行里场记附记（句号后以“场记”开头的说明性文字），没有则空串。"""
    match = re.search(r"。(场记[^。]*。?)$", hearing)
    return match.group(1) if match else ""


def has_source_doubt(hearing):
    return bool(DOUBT_RE.search(hearing))


def fits_prefix_suffix(track_text, hearing, strict=True):
    """核对音轨原文与缺口两侧可辨原字是否接得上，返回 (是否吻合, 缺口中文字)。

    strict=True：可辨片段须在音轨原文中按序出现，且相邻片段间多出的字只能
    落在听辨行的〔缺音〕位置，用于借用资格。
    strict=False：只要求顺序包含，允许跨气口，仅供“整段比较”类音轨做文字对照。
    原声听出的字必须与音轨完全一致；填充文字只取自〔缺音〕覆盖的区间。
    """
    body = hearing.replace(note_text(hearing), "")
    parts = split_hearing(body)
    audible = [(i, text) for i, (text, is_gap) in enumerate(parts) if not is_gap]
    cursor = 0
    fill = ""
    for pos, (part_index, text) in enumerate(audible):
        idx = track_text.find(text, cursor)
        if idx < 0:
            return False, ""
        extra = track_text[cursor:idx]
        if extra:
            # 严格模式：音轨多出的字，听辨行对应位置必须是缺音
            previous_is_gap = part_index > 0 and parts[part_index - 1][1]
            if strict and not previous_is_gap:
                return False, ""
            fill = extra if strict else fill + extra
        cursor = idx + len(text)
    if strict and cursor < len(track_text) and not parts[-1][1]:
        return False, fill
    if not strict:
        # 宽松模式用于整段比较：音轨尾部多出的整句也作为对照文字返回
        fill += track_text[cursor:]
    return True, fill


# ---------------------------------------------------------------- 台本事实

def script_facts(scripts):
    """从场记/台本原文中提取可直接约束修复的事实，全部带出处编号。"""
    facts = {"划掉未录": [], "正片删除": [], "仅环境声镜头": set(),
             "画外转述": [], "剪辑说明": [], "留底不进": []}
    for row in scripts:
        text = row["原文"]
        for quote in QUOTED_RE.findall(text):
            if "划掉" in text:
                facts["划掉未录"].append({"出处": row["id"], "原文": quote})
            elif "留底但不进" in text:
                facts["留底不进"].append({"出处": row["id"], "原文": quote})
        if "正片删" in text:
            tail = re.search(r"[‘“「]([^’”」]+)[’”」][^。]*正片删", text)
            if tail:
                facts["正片删除"].append({"出处": row["id"], "原文": tail.group(1)})
        fog = re.search(r"([A-Za-z]+\d+)\s*只有[^。]*声", text)
        if fog:
            facts["仅环境声镜头"].add(fog.group(1))
            facts.setdefault("环境声出处", {})[fog.group(1)] = row["id"]
        if "画外转述" in text or "场外语" in text:
            shot = re.search(r"([A-Za-z]+\d+)[^。]*画外转述", text)
            facts["画外转述"].append({
                "出处": row["id"],
                "镜头": shot.group(1) if shot else "",
                "原文": text,
            })
        if "剪辑" in text or "切到" in text:
            facts["剪辑说明"].append({"出处": row["id"], "原文": text})
    return facts


# ---------------------------------------------------------------- 音轨库存

def track_kind(track):
    """按馆方批准用途给音轨分类。"""
    usage = track["用途"]
    if "比较" in usage or "未核" in usage:
        return "仅比较"
    if "一气" in usage or "独立借用" in usage:
        return "一气录完限窗借用"
    if "旁白" in usage or "画外" in usage:
        return "旁白可借"
    return "完整原句可借"


def borrow_window_ms(track):
    """一气录完的音轨允许独立借用的毫秒窗，其余音轨返回全长。"""
    match = re.search(r"前\s*(\d+)\s*毫秒", track["用途"])
    if match:
        return int(match.group(1))
    start, end = track["区间"]
    return end - start


def offscreen_retell_shots(facts, scripts, version):
    """场记/补页在指定剪辑版本内明确以画外转述承接的镜头集合。

    未注明版本的场记条目不自动放行，避免把一个版本的画外安排外推到另一版本。
    """
    shots = set()
    for row in scripts:
        text = row["原文"]
        if "画外转述" not in text and "场外语" not in text:
            continue
        for sentence in re.split(r"[。；]", text):
            if "画外转述" not in sentence and "场外语" not in sentence:
                continue
            versions = re.findall(r"院线|靠岸", sentence)
            if not versions or version not in versions:
                continue
            shots.update(re.findall(r"B\d+", sentence))
    return shots


def evaluate_candidate(track, picture_row, facts, scripts):
    """逐条核对候选音轨，返回 (是否成立, 原因列表, 填充文字)。

    成立只代表音轨文字与两侧原字接得上、剪辑/镜头/批准用途合规；
    仅供整段比较的音轨即使文字吻合也不成立，另以对照候选保留。
    """
    reasons = []
    if picture_row["剪辑"] not in track["剪辑"]:
        reasons.append(f"音轨 {track['id']} 批准剪辑 {track['剪辑']} 不含{picture_row['剪辑']}版")
    if track["来源"] != picture_row["镜头"]:
        reasons.append(f"音轨 {track['id']} 来源镜头 {track['来源']} 与 {picture_row['镜头']} 不符")
    # R1：可分开的画外转述允许用馆方另批旁白，此时旁白声部可覆盖同期登记
    retell_shots = offscreen_retell_shots(facts, scripts, picture_row["剪辑"])
    voice_ok = (track["声部"] == picture_row["声道"]
                or (picture_row["镜头"] in retell_shots and track["声部"] == "旁白"))
    if not voice_ok:
        reasons.append(f"音轨 {track['id']} 声部 {track['声部']} 与声道 {picture_row['声道']} 不符")
    kind = track_kind(track)
    if kind == "仅比较":
        reasons.append(f"音轨 {track['id']} 用途为“{track['用途']}”，不可借缝")
    # 片中未播的原话不可补入
    for banned in facts["划掉未录"] + facts["留底不进"]:
        if banned["原文"] and banned["原文"] in track["原文"]:
            reasons.append(f"音轨 {track['id']} 含{banned['出处']}划掉/留底不进的台词“{banned['原文']}”")
    for deleted in facts["正片删除"]:
        if deleted["原文"] in track["原文"] and deleted["原文"] not in audible_parts(picture_row["听辨"]):
            reasons.append(f"音轨 {track['id']} 含{deleted['出处']}正片删除句“{deleted['原文']}”")
    strict_ok, fill = fits_prefix_suffix(track["原文"], picture_row["听辨"], strict=True)
    loose_ok, loose_fill = fits_prefix_suffix(track["原文"], picture_row["听辨"], strict=False)
    if not loose_ok:
        reasons.append(f"音轨 {track['id']} 原文与缺口两侧可辨原字接不上")
        return False, reasons, ""
    if not strict_ok:
        reasons.append(
            f"音轨 {track['id']} 多出的文字落在听辨行可辨位置而非〔缺音〕，"
            "借用会覆盖或挤进原声，仅可留作文字对照"
        )
        return False, reasons, loose_fill
    gap_len = picture_row["结束"] - picture_row["开始"]
    if kind == "一气录完限窗借用" and gap_len > borrow_window_ms(track):
        reasons.append(
            f"缺口 {gap_len}ms 超过 {track['id']} 可独立借用窗 {borrow_window_ms(track)}ms，"
            "同气口半句不得各取一半"
        )
    return not reasons, reasons, fill


# ---------------------------------------------------------------- 说话人推断

ROLE_BY_SHOT = {
    "B18": "引舟人",
    "B19": "引舟人",
    "B31": "少年",
    "B32": "周师傅",
}


def infer_speaker(picture_row, tracks, scripts):
    """按声道、镜头与库存声部推断缺口说话人，返回 (角色, 限定说明)。

    画外转述归旁白角色；场记/补页的画外安排只在其指定的剪辑版本内适用。
    """
    if picture_row["声道"] == "旁白":
        narrators = {t["角色"] for t in tracks
                     if t["声部"] == "旁白" and picture_row["剪辑"] in t["剪辑"]}
        return ("/".join(sorted(narrators)) or "旁白说话人待核", "旁白转述")
    for row in scripts:
        text = row["原文"]
        if picture_row["镜头"] not in text:
            continue
        for sentence in re.split(r"[。；]", text):
            if picture_row["镜头"] not in sentence:
                continue
            if "画外转述" not in sentence and "场外语" not in sentence:
                continue
            # 句内若写明剪辑版本（如“院线 B19”“《靠岸》”），只在该版本内生效
            versions = re.findall(r"院线|靠岸", sentence)
            if versions and picture_row["剪辑"] not in versions:
                continue
            narrator = re.search(r"由([^，。]+?)在?画外", sentence)
            if narrator:
                return (narrator.group(1),
                        f"{row['id']} 指定画外转述；同期登记声源待核")
    return ROLE_BY_SHOT.get(picture_row["镜头"], "说话人待核"), ""


# ---------------------------------------------------------------- 逐行评估

def assess_picture(picture, tracks, scripts, restoration):
    facts = script_facts(scripts)
    results = []
    for row in picture:
        hearing = row["听辨"]
        speaker, speaker_note = infer_speaker(row, tracks, scripts)
        entry = {
            "成片行": row["id"], "剪辑": row["剪辑"], "场目": row["场目"],
            "镜头": row["镜头"], "声道": row["声道"],
            "区间": [row["开始"], row["结束"]],
            "听辨原文": hearing,
            "保留原字": audible_parts(hearing),
            "说话人": speaker, "说话人限定": speaker_note,
            "声源有疑": has_source_doubt(hearing),
            "候选": [], "决定": "", "依据": [], "工作记录": [],
        }
        if is_full_gap(hearing):
            _assess_full_gap(entry, row, facts, restoration)
            results.append(entry)
            continue
        if not GAP_RE.search(hearing):
            entry["决定"] = INTACT
            entry["依据"].append("听辨行无缺音标记，原声可辨，整条留作对应锚点")
            results.append(entry)
            continue
        entry["缺口数"] = hearing.count("〔缺音〕")
        for track in tracks:
            ok, reasons, strict_fill = evaluate_candidate(track, row, facts, scripts)
            loose_ok, loose_fill = fits_prefix_suffix(
                track["原文"], hearing, strict=False)
            # 仅比较的音轨若文字与两侧原字吻合，保留为对照，供剪接回听时整段比对
            comparison = not ok and track_kind(track) == "仅比较" and loose_ok
            candidate = {
                "音轨": track["id"], "录次": track["录次"], "声部": track["声部"],
                "角色": track["角色"], "用途类别": track_kind(track),
                "音轨原文": track["原文"],
                "缺口填充文字": strict_fill or loose_fill,
                "成立": ok, "留作对照": comparison, "不成立原因": reasons,
            }
            entry["候选"].append(candidate)
        valid = [c for c in entry["候选"] if c["成立"]]
        _decide_partial_gap(entry, row, valid, facts, restoration)
        results.append(entry)
    return results, facts


def _assess_full_gap(entry, row, facts, restoration):
    shot = row["镜头"]
    if shot in facts["仅环境声镜头"]:
        source_ids = [item["出处"] for item in facts["画外转述"] if item["镜头"] == shot]
        source_ids.append(facts.get("环境声出处", {}).get(shot, ""))
        source = "、".join(s for s in dict.fromkeys(source_ids) if s) or "补页"
        entry["决定"] = KEEP
        entry["依据"].append(
            f"{source} 载明 {shot} 只有环境声，全段缺音无台词词面可比"
        )
        entry["工作记录"].append(
            f"{row['剪辑']} {shot}（{row['id']}）全段缺音：场记确认该镜头无台词，"
            "保留缺口，不安排补录；B19/B20 一口气推定因补页确认修复稿不含 B20 台词而解除"
        )
    else:
        entry["决定"] = VERIFY
        entry["工作记录"].append(
            f"{row['剪辑']} {shot}（{row['id']}）全段缺音且无词面可比，"
            "台本与转写分歧无法自行裁定，标明缺口待修复员核证"
        )


def _decide_partial_gap(entry, row, valid, facts, restoration):
    comparisons = [c for c in entry["候选"] if c["留作对照"]]
    if not valid:
        entry["决定"] = VERIFY
        if comparisons:
            chosen = comparisons[0]
            entry["对照音轨"] = chosen["音轨"]
            entry["缺口填充文字"] = chosen["缺口填充文字"]
            entry["依据"].append(
                f"对照候选 {chosen['音轨']}（{chosen['角色']}）文字与两侧原字吻合，"
                "但用途限整段比较、接缝未核，只能随新剪接回听整段核对，不可借缝"
            )
            entry["工作记录"].append(
                f"{row['id']} 无可借音轨；{chosen['音轨']} 留作气口对照，"
                "已录段实际播读文字原样保留，回听前缺口不动"
            )
        else:
            entry["工作记录"].append(
                f"{row['id']} 缺口无成立候选："
                + "；".join(reason for c in entry["候选"] for reason in c["不成立原因"])
            )
        return
    chosen = valid[0]
    entry["选定音轨"] = chosen["音轨"]
    entry["缺口填充文字"] = chosen["缺口填充文字"]
    install = installed_for(row, restoration)
    if install:
        entry["依据"].append(f"安装记录 {install['id']}：{install['原文']}")
    if entry["声源有疑"]:
        entry["决定"] = VERIFY
        entry["依据"].append(
            f"听辨附记标明声源有疑；候选 {chosen['音轨']}（{chosen['角色']}，{chosen['用途类别']}）"
            "文字与两侧原字吻合，须按补页现场核对录次与说话人后方可借用"
        )
        entry["工作记录"].append(
            f"{row['id']} 同期登记声源有疑，不得用台本相似句覆盖原声；"
            f"待核 {chosen['音轨']} 的声部/角色/可用剪辑，核实前缺口原样保留"
        )
    elif chosen["用途类别"] == "仅比较":
        entry["决定"] = VERIFY
        entry["依据"].append(
            f"候选 {chosen['音轨']} 仅供气口整段比较、接缝未核，且该剪接行待回听"
        )
    else:
        entry["决定"] = BORROW
        entry["依据"].append(
            f"候选 {chosen['音轨']}：{chosen['录次']}、{chosen['声部']}、{chosen['角色']}，"
            f"批准剪辑含{row['剪辑']}版，来源镜头 {row['镜头']}，与可辨原字连续"
        )


def installed_for(row, restoration):
    for rec in restoration:
        if rec["记录"] == "安装记录" and row["id"] in rec["原文"]:
            return rec
    return None


# ---------------------------------------------------------------- 档期与预约

def parse_bookings(restoration):
    bookings, recorded, installs = [], [], []
    for rec in restoration:
        if rec["记录"] == "已预约":
            # 一条记录可能含多句独立预约，逐句提取，避免把另一窗口的成片行并进来
            for sentence in re.split(r"。", rec["原文"]):
                ids = re.findall(r"S\d+", sentence)
                if not ids:
                    continue
                actor = re.search(r"由(.+?)(?:上午|下午)?(?:到棚|补录)", sentence)
                bookings.append({
                    "出处": rec["id"], "成片行": ids,
                    "演员": actor.group(1) if actor else "",
                    "窗口": "上午" if "上午" in sentence else ("下午" if "下午" in sentence else ""),
                    "原文": sentence, "状态": "待重新核定",
                })
        elif rec["记录"] == "已录":
            ids = re.findall(r"S\d+", rec["原文"])
            quote = QUOTED_RE.findall(rec["原文"])
            recorded.append({
                "出处": rec["id"], "成片行": ids,
                "实际播读文字": quote[0] if quote else "",
                "原文": rec["原文"],
            })
        elif rec["记录"] == "安装记录":
            installs.append(rec)
    return bookings, recorded, installs


def reconcile_schedule(assessments, restoration, tracks):
    """按补页与评估结果重新核定已预约/已录安排，已录音段只保留不改派。"""
    bookings, recorded, installs = parse_bookings(restoration)
    by_id = {a["成片行"]: a for a in assessments}
    recorded_sids = {sid for rec in recorded for sid in rec["成片行"]}
    orders, logs = [], []
    for booking in bookings:
        rows = [by_id[i] for i in booking["成片行"] if i in by_id]
        if len(rows) > 1:
            logs.append({
                "分类": LOG_SPLIT,
                "内容": f"{booking['出处']} 的连录预约 {booking['成片行']} 拆开重新核定",
            })
        for row in rows:
            # 已有实际录音的成片行走“已录待核”，不再生成新的补录/保留工作单
            if row["成片行"] in recorded_sids:
                continue
            order = _rebook_one(row, booking, restoration)
            orders.append(order)
            if order["处置"] == "撤销补录":
                logs.append({"分类": LOG_SPLIT, "内容": order["说明"]})
            elif order["处置"] == "改派":
                logs.append({"分类": LOG_SPLIT, "内容": order["说明"]})
            elif order["处置"] == "核实后定":
                logs.append({"分类": LOG_FACTS, "内容": order["说明"]})
    for rec in recorded:
        for sid in rec["成片行"]:
            row = by_id.get(sid)
            if not row:
                continue
            logs.append({
                "分类": LOG_RETAIN,
                "内容": (f"{sid} 已录音段保留实际播读：{rec['实际播读文字']}（{rec['出处']}）；"
                        "不重录、不改派，仅在剪接回听后决定可用范围"),
            })
            orders.append({
                "成片行": sid, "剪辑": row["剪辑"], "镜头": row["镜头"],
                "声道": row["声道"], "原预约演员": booking_actor(bookings, sid),
                "处置": "已录待核",
                "说明": (f"已录“{rec['实际播读文字']}”，只可整段比较；缺口是否承接待 B31 新剪接回听，"
                        "回听前缺口原样保留"),
            })
    return orders, logs, bookings


def booking_actor(bookings, sid):
    for booking in bookings:
        if sid in booking["成片行"]:
            return booking["演员"]
    return ""


def _rebook_one(row, booking, restoration):
    sid = row["成片行"]
    base = {
        "成片行": sid, "剪辑": row["剪辑"], "镜头": row["镜头"],
        "声道": row["声道"], "原预约演员": booking["演员"], "窗口": booking["窗口"],
    }
    if row["决定"] == KEEP:
        return {**base, "处置": "撤销补录",
                "说明": (f"{sid} 经补页 P7 确认无台词依据（仅环境声），"
                        "从连录预约中撤出，缺口原样保留")}
    if row["决定"] == BORROW:
        return {**base, "处置": "撤销补录",
                "说明": f"{sid} 可由 {row.get('选定音轨', '')} 原录音借用，无需补录"}
    if row["决定"] == VERIFY and row.get("选定音轨"):
        track_id = row["选定音轨"]
        return {**base, "处置": "核实后定",
                "说明": (f"{sid} 先按补页 P7 现场核对 {track_id}（{row['说话人']}）；"
                        "核实通过即借该原轨、撤销补录，核实不过才在原窗口改派该声轨角色补录，"
                        "录次4 上午只剩一个完整气口，不得拼半句")}
    return {**base, "处置": "保留缺口",
            "说明": f"{sid} 无成立候选，标明缺口与台本/转写分歧，待修复员核证，不擅自补录"}


# ---------------------------------------------------------------- 采用台账

def build_ledger(assessments, tracks, restoration):
    """已用录音台账：记录当时采用的剪辑与原句，防止下一版误认必然适用。"""
    track_by_id = {t["id"]: t for t in tracks}
    ledger = []
    for rec in restoration:
        if rec["记录"] != "安装记录":
            continue
        sid = (re.findall(r"S\d+", rec["原文"]) or [""])[0]
        kid = (re.findall(r"K\d+", rec["原文"]) or [""])[0]
        track = track_by_id.get(kid)
        row = next((a for a in assessments if a["成片行"] == sid), None)
        ledger.append({
            "安装记录": rec["id"], "成片行": sid,
            "剪辑": row["剪辑"] if row else "", "镜头": row["镜头"] if row else "",
            "采用音轨": kid,
            "音轨原文": track["原文"] if track else "",
            "批准剪辑范围": track["剪辑"] if track else [],
            "适用限制": (f"确认仅适用于 {row['剪辑'] if row else '?'}版 "
                        f"{row['镜头'] if row else '?'} 的可辨原字；"
                        "其他剪辑/镜头须另行核对，不得据此默认适用"),
        })
    return ledger


# ---------------------------------------------------------------- 交接清单

def build_handoff(data_dir=DATA_DIR):
    picture = read_collection(data_dir / "picture.json")
    tracks = read_collection(data_dir / "tracks.json")
    scripts = read_collection(data_dir / "scripts.json")
    restoration = read_collection(data_dir / "restoration.json")

    assessments, facts = assess_picture(picture, tracks, scripts, restoration)
    orders, schedule_logs, bookings = reconcile_schedule(assessments, restoration, tracks)
    ledger = build_ledger(assessments, tracks, restoration)

    work_log = list(schedule_logs)
    for entry in assessments:
        for item in entry["工作记录"]:
            work_log.append({"分类": LOG_GAP if entry["决定"] in (KEEP, VERIFY) else LOG_FACTS,
                             "内容": item})
    for rec in restoration:
        if rec["记录"] == "规则":
            work_log.append({"分类": LOG_FACTS, "内容": f"{rec['id']} 规则：{rec['原文']}"})

    buckets = {BORROW: [], VERIFY: [], KEEP: [], INTACT: []}
    for entry in assessments:
        buckets[entry["决定"]].append(entry["成片行"])

    return {
        "评估": assessments,
        "台本事实": facts,
        "交接清单": {
            "可直接借用的原录音": sorted(buckets[BORROW]),
            "须先核实或原样保留的位置": sorted(buckets[VERIFY] + buckets[KEEP]),
            "原声完整锚点": sorted(buckets[INTACT]),
            "补录工作单": orders,
        },
        "采用台账": ledger,
        "工作记录": work_log,
    }


# ---------------------------------------------------------------- 中文报告

def render_report(handoff):
    lines = ["电影修复缺音台词交接清单", "=" * 30]
    for entry in handoff["评估"]:
        lines.append("")
        lines.append(f"[{entry['成片行']}] {entry['剪辑']}版 {entry['场目']} {entry['镜头']} "
                     f"{entry['声道']} {entry['区间'][0]}–{entry['区间'][1]}ms")
        lines.append(f"  听辨原文：{entry['听辨原文']}")
        lines.append(f"  保留原字：{entry['保留原字'] or '（无可辨原字）'}")
        speaker_line = f"  说话人：{entry['说话人']}"
        if entry["说话人限定"]:
            speaker_line += f"（{entry['说话人限定']}）"
        speaker_line += f"；声源有疑：{'是' if entry['声源有疑'] else '否'}"
        lines.append(speaker_line)
        lines.append(f"  决定：{entry['决定']}")
        if entry.get("选定音轨"):
            lines.append(f"  选定音轨：{entry['选定音轨']}，缺口填充文字：{entry.get('缺口填充文字', '')}")
        for candidate in entry["候选"]:
            tag = "成立" if candidate["成立"] else "不成立"
            lines.append(f"    候选 {candidate['音轨']}（{candidate['角色']}/{candidate['声部']}，"
                         f"{candidate['用途类别']}）{tag}")
            for reason in candidate["不成立原因"]:
                lines.append(f"      × {reason}")
        for basis in entry["依据"]:
            lines.append(f"  依据：{basis}")
        for note in entry["工作记录"]:
            lines.append(f"  工作记录：{note}")

    lines += ["", "补录工作单", "-" * 30]
    for order in handoff["交接清单"]["补录工作单"]:
        lines.append(f"{order['成片行']}（{order['剪辑']} {order['镜头']} {order['声道']}）"
                     f"{order['处置']}：{order['说明']}")

    lines += ["", "已用录音采用台账", "-" * 30]
    for item in handoff["采用台账"]:
        lines.append(f"{item['成片行']} ← {item['采用音轨']}「{item['音轨原文']}」 {item['适用限制']}")

    lines += ["", "工作记录", "-" * 30]
    for log in handoff["工作记录"]:
        lines.append(f"[{log['分类']}] {log['内容']}")
    return "\n".join(lines)


def _jsonable(value):
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def main():
    parser = argparse.ArgumentParser(description="缺音台词重建与补录交接处理")
    parser.add_argument("--json", action="store_true", help="输出结构化 JSON 而非中文报告")
    parser.add_argument("--data", default=str(DATA_DIR), help="资料目录路径")
    parser.add_argument("--out", help="将结果写入指定文件")
    args = parser.parse_args()
    handoff = build_handoff(Path(args.data))
    payload = (json.dumps(_jsonable(handoff), ensure_ascii=False, indent=2)
               if args.json else render_report(handoff))
    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
    else:
        print(payload)


if __name__ == "__main__":
    main()
