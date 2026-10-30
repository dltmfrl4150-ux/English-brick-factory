"""
브릭 학습 모드 — 새 형식 CSV 읽기 · 검증 · 블록 색상 지정

CSV 형식 (한 행 = 블록 하나, UTF-8)
  topic, target, situation, learn_lang      : 파일 첫 행에만
  group, sentence_id, speaker               : 모든 행
  block_order, block_text, block_meaning, category : 모든 행
  full_sentence, full_translation, direction : 각 문장의 첫 블록 행에만
  needs_review                              : 확인이 필요한 블록만 'Y'

사용법:
    python brick_csv.py <CSV경로>     # 검증 결과와 문장/블록 요약 출력
"""
import csv
import sys
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

# ───────── 색상 (컬러 시안 기준) ─────────
# 진한색 = 기본색. 한 문장에서 같은 역할이 연속으로 나올 때만 연한색을 섞음 (앞쪽이 연한색).
PALETTE = {
    "RED":    {"main": "#A1281F", "light": "#C13535"},
    "ORANGE": {"main": "#BE4D14", "light": "#EA7236"},
    "YELLOW": {"main": "#B18925", "light": "#D7AB3D"},
    "GREEN":  {"main": "#2E5439", "light": "#478158"},
    "BLUE":   {"main": "#215F9A", "light": "#4E95D9"},
    "PURPLE": {"main": "#5D2884", "light": "#873AC0"},
    "GRAY":   {"main": "#595959", "light": "#595959"},   # 회색은 연한색 없음
}
CATEGORIES = tuple(PALETTE)
LEARN_LANGS = ("en", "ko")

REQUIRED_COLUMNS = ("group", "sentence_id", "speaker", "block_order",
                    "block_text", "block_meaning", "category")
TOPIC_COLUMNS = ("topic", "target", "situation", "learn_lang")
SENTENCE_COLUMNS = ("full_sentence", "full_translation", "direction")


@dataclass
class Block:
    order: int
    text: str            # 학습 언어 표현 (블록 안에 공개)
    meaning: str         # 뜻 (블록 아래 표시, 누적 음성)
    category: str
    shade: str = "main"  # main | light
    needs_review: bool = False

    @property
    def color(self) -> str:
        return PALETTE[self.category][self.shade]


@dataclass
class Sentence:
    sid: int
    group: str
    speaker: str
    full: str            # 학습 언어 전체 문장
    translation: str     # 뜻 언어 전체 번역
    direction: str
    blocks: list = field(default_factory=list)

    def prefix(self, k: int) -> str:
        """1~k번 블록을 이은 누적 학습 문장 (k번째 공개 시 읽는 음성)."""
        return " ".join(b.text for b in self.blocks[:k])

    def meaning_prefix(self, k: int, sep: str = ", ") -> str:
        """1~k번 블록 뜻을 이은 누적 뜻 음성용 텍스트. 블록 뜻과 전체 번역은 섞지 않음.
        화면 표기용 '~' (예: '~도')는 음성에서 읽지 않도록 제거."""
        return sep.join(b.meaning.replace("~", "").strip() for b in self.blocks[:k])


@dataclass
class Topic:
    topic: str
    target: str
    situation: str
    learn_lang: str
    sentences: list = field(default_factory=list)

    def groups(self) -> "OrderedDict[str, list]":
        """CSV에 나온 순서대로 그룹별 문장 목록 (그룹 하나 = 롱폼 영상 하나)."""
        out = OrderedDict()
        for s in self.sentences:
            out.setdefault(s.group, []).append(s)
        return out


class BrickCSVError(ValueError):
    def __init__(self, problems):
        self.problems = problems
        super().__init__("CSV 검증 실패:\n" + "\n".join(f"  - {p}" for p in problems))


def is_brick_csv(path) -> bool:
    """헤더에 block_text 열이 있으면 새 형식(브릭 모드)."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        header = next(csv.reader(f), [])
    return "block_text" in [h.strip() for h in header]


def assign_shades(blocks):
    """같은 카테고리가 연속으로 이어지는 구간에서만 연한색/진한색을 번갈아 지정.
    구간의 첫 블록이 연한색 (2개: 연한-진한, 3개: 연한-진한-연한). 단독 블록은 진한색."""
    i = 0
    while i < len(blocks):
        j = i
        while j + 1 < len(blocks) and blocks[j + 1].category == blocks[i].category:
            j += 1
        run = blocks[i:j + 1]
        for n, b in enumerate(run):
            b.shade = "main" if len(run) == 1 or b.category == "GRAY" else ("light" if n % 2 == 0 else "main")
        i = j + 1


def load_brick_csv(path) -> Topic:
    path = Path(path)
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = [{(k or "").strip(): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(f)]

    problems = []
    if not rows:
        raise BrickCSVError(["데이터 행이 없습니다."])
    missing = [c for c in REQUIRED_COLUMNS + TOPIC_COLUMNS + SENTENCE_COLUMNS if c not in rows[0]]
    if missing:
        raise BrickCSVError([f"필수 열 없음: {', '.join(missing)}"])

    first = rows[0]
    topic = Topic(first["topic"], first["target"], first["situation"], first["learn_lang"].lower())
    if not topic.topic:
        problems.append("첫 행에 topic이 없습니다.")
    if topic.learn_lang not in LEARN_LANGS:
        problems.append(f"learn_lang은 en 또는 ko여야 합니다 (현재: '{topic.learn_lang}').")

    by_id = OrderedDict()
    for line_no, r in enumerate(rows, start=2):          # 엑셀 기준 행 번호 (1행 = 헤더)
        if not any(r.values()):
            continue
        try:
            sid, order = int(r["sentence_id"]), int(r["block_order"])
        except ValueError:
            problems.append(f"{line_no}행: sentence_id / block_order가 숫자가 아닙니다.")
            continue
        cat = r["category"].upper()
        if cat not in CATEGORIES:
            problems.append(f"{line_no}행: 알 수 없는 category '{r['category']}' (허용: {', '.join(CATEGORIES)})")
        if not r["block_text"]:
            problems.append(f"{line_no}행: block_text가 비어 있습니다.")
        if not r["block_meaning"]:
            problems.append(f"{line_no}행: block_meaning이 비어 있습니다.")

        s = by_id.get(sid)
        if s is None:
            s = by_id[sid] = Sentence(sid, r["group"], r["speaker"], r["full_sentence"],
                                      r["full_translation"], r["direction"])
        else:
            if r["group"] and r["group"] != s.group:
                problems.append(f"{line_no}행: 문장 {sid}의 group이 앞 행과 다릅니다.")
            for key, attr in (("full_sentence", "full"), ("full_translation", "translation"),
                              ("direction", "direction")):
                if r[key] and not getattr(s, attr):
                    setattr(s, attr, r[key])
        s.blocks.append(Block(order, r["block_text"], r["block_meaning"], cat,
                              needs_review=r.get("needs_review", "").upper() == "Y"))

    for s in by_id.values():
        s.blocks.sort(key=lambda b: b.order)
        orders = [b.order for b in s.blocks]
        if orders != list(range(1, len(orders) + 1)):
            problems.append(f"문장 {s.sid}: block_order가 1부터 연속되지 않습니다 {orders}")
        if not s.full:
            problems.append(f"문장 {s.sid}: full_sentence가 없습니다.")
        elif s.prefix(len(s.blocks)) != s.full:
            problems.append(f"문장 {s.sid}: 블록을 이은 문장이 full_sentence와 다릅니다\n"
                            f"      블록: {s.prefix(len(s.blocks))}\n      전체: {s.full}")
        if not s.translation:
            problems.append(f"문장 {s.sid}: full_translation이 없습니다.")
        if not s.group:
            problems.append(f"문장 {s.sid}: group이 없습니다.")
        if not s.speaker:
            problems.append(f"문장 {s.sid}: speaker가 없습니다.")
        assign_shades(s.blocks)

    if problems:
        raise BrickCSVError(problems)
    topic.sentences = list(by_id.values())
    return topic


SHORT = {"RED": "빨", "ORANGE": "주", "YELLOW": "노", "GREEN": "초", "BLUE": "파", "PURPLE": "보", "GRAY": "회"}


def summary(t: Topic) -> str:
    lines = [f"주제: {t.topic} / 대상: {t.target} / 학습 언어: {t.learn_lang}",
             f"상황: {t.situation}"]
    for g, ss in t.groups().items():
        lines.append(f"\n[{g}] {len(ss)}문장, 블록 {sum(len(s.blocks) for s in ss)}개")
        for s in ss:
            parts = " | ".join(f"{b.text}({SHORT[b.category]}{'*' if b.shade == 'light' else ''})" for b in s.blocks)
            lines.append(f"  {s.sid:>2}. {s.speaker}: {parts}")
    review = [(s.sid, b.text) for s in t.sentences for b in s.blocks if b.needs_review]
    lines.append(f"\n확인 필요 블록: {len(review)}개" + "".join(f"\n  문장 {i}: {x}" for i, x in review))
    lines.append("(* = 연한색)")
    return "\n".join(lines)


if __name__ == "__main__":
    for p in sys.argv[1:]:
        print(f"===== {p}")
        try:
            print(summary(load_brick_csv(p)))
        except BrickCSVError as e:
            print(e)
            sys.exit(1)
