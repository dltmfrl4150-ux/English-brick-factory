"""
영어브릭 섀도잉 영상 렌더러

CSV -> Edge TTS -> pydub 타임라인 합성 -> PIL 정지 프레임 -> ffmpeg MP4

- 16:9 롱폼 (1920x1080): 전체 챕터를 한 영상으로
- 9:16 숏폼 (1080x1920): 연령대(챕터)별 3개 영상

흐름: [챕터 타이틀 3초]
      -> Phase1 듣기 (전체 대화 리스트 + 현재 문장 하이라이트)
      -> Phase2 따라 말하기 (문장별: 영어 -> 2배 대기 -> 영어 -> 2배 대기)
      -> Phase3 영작하기 (문장별: 한국어 -> 2배 대기 -> 영어 정답, 2사이클)
      -> Phase4 동시 섀도잉 (Phase1과 동일)
      ... 마지막에 [아웃트로 5초]

사용법:
    python shadowing_render.py [CSV경로] [--format long|shorts] [-o 출력폴더]
"""
import argparse
import asyncio
import csv
import hashlib
import json
import math
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import edge_tts
import numpy as np
from PIL import Image, ImageColor, ImageDraw, ImageFont
from pydub import AudioSegment
from pydub.silence import detect_leading_silence

# ───────────────────────── 설정 ─────────────────────────
ROOT = Path(__file__).resolve().parent
DEFAULT_CSV = ROOT.parent / "0_script" / "영어브릭 대화문_아침깨우기.csv"
DEFAULT_OUT = ROOT / "output"
BUILD = ROOT / "build"
TTS_DIR = BUILD / "tts"
FRAME_DIR = BUILD / "frames"

FPS = 30
SAMPLE_RATE = 24000
SHORTS_MAX_SEC = 180             # YouTube Shorts 자동 분류 상한

BG = "#000000"
TITLE_BG = "#000000"
TEXT_EN = "#FFFFFF"
MIC_COLOR = "#6E6E6E"
KO_COLOR = "#747D8C"
LABEL_COLOR = "#8A8F98"

# 역할 키 -> (언어, 화자). config.json의 voices 항목과 같은 키를 씀.
VOICE_ROLES = {
    "en_mom": ("en", "엄마"),
    "en_kid": ("en", "아이"),
    "ko_mom": ("ko", "엄마"),
    "ko_kid": ("ko", "아이"),
}
DEFAULT_VOICES = {
    "en_mom": "en-US-JennyNeural",
    "en_kid": "en-US-AnaNeural",
    "ko_mom": "ko-KR-SunHiNeural",
    "ko_kid": "ko-KR-SunHiNeural",
}

# 쨍한 순색 팔레트 (검은 배경 + 테두리 전용)
PALETTE = {
    "빨강": "#FF0000", "주황": "#FF6600", "노랑": "#FFFF00", "초록": "#00FF00",
    "파랑": "#0066FF", "남색": "#4B00FF", "보라": "#CC00FF",
    # 보조/분기용
    "핑크": "#FF007F", "연보라": "#DDA0DD", "하늘": "#00FFFF", "연두": "#ADFF2F",
}
# CSV 열 제목 접두어 -> 테두리 색. 화면 브릭 순서는 CSV 열 순서를 따름.
BRICK_COLUMNS = [(name, color) for name, color in PALETTE.items()] + [
    ("회색", PALETTE["남색"]),     # 접속사 열: 무지개 6번째 자리 = 남색
]

CHAPTERS = [("영아", "영아 (0~3세)"), ("유아", "유아 (4~7세)"), ("초등", "초등 이상 (8세~)")]

ROOM_TONE_DBFS = -64             # 무음 구간을 채우는 들리지 않는 잡음 레벨 (재생 장치 절전 방지)

# 타이밍 프로필 (ms). apply_timing()으로 모듈 전역에 적용.
#   ROUNDS: Phase 2/3 문장당 반복 횟수
#   LEAD_PAD_MS: 모든 음성 클립 앞 무음 패딩 (첫 음절 잘림 방지)
#   WAIT_FACTOR: 섀도잉 대기 = 직전 음성 길이 x 배수
#   HOLD_AFTER_VOICE_MS: 음성 종료 후 화면 유지 시간 (이후에야 다음 화면으로 전환)
#   PHASE_CARD_MS: 페이즈 안내 카드 (0이면 생략)
TIMING_PROFILES = {
    "full": dict(ROUNDS=2, LEAD_PAD_MS=1000, WAIT_FACTOR=2.0, HOLD_AFTER_VOICE_MS=1000,
                 CHAPTER_TITLE_MS=3000, PHASE_CARD_MS=1500, OUTRO_MS=5000),
    # 숏폼 3분 압축: 모든 Phase 유지, 반복·여백만 축소
    "compact": dict(ROUNDS=1, LEAD_PAD_MS=500, WAIT_FACTOR=2.0, HOLD_AFTER_VOICE_MS=500,
                    CHAPTER_TITLE_MS=1500, PHASE_CARD_MS=0, OUTRO_MS=3000),
}
INTRO_MIN_MS = 10000             # 롱폼 인트로 최소 길이 (유튜브 챕터 최소 10초)
INTRO_TEXT = "오늘의 주제는 {topic}입니다. 영아, 유아, 초등 이상 순서로 함께 연습해 볼게요."
COMPACT_TARGET_SEC = 175        # 압축 모드 목표 길이 (Shorts 상한 180초에 여유)
COMPACT_MIN_WAIT = 1.5           # 목표를 넘을 때만 대기 배수를 이 값까지 자동으로 낮춤


def apply_timing(name, **overrides):
    globals().update(TIMING_PROFILES[name], **overrides)


apply_timing("full")

FONT_EN = "C:/Windows/Fonts/segoeuib.ttf"
FONT_KO = "C:/Windows/Fonts/malgunbd.ttf"
FONT_KO_REG = "C:/Windows/Fonts/malgun.ttf"
FONT_EMOJI = "C:/Windows/Fonts/seguiemj.ttf"

BRICK_PAD_X, BRICK_PAD_Y = 30, 22
BRICK_GAP, LINE_GAP = 0, 26      # 브릭끼리 맞붙임 (가로 간격 0)
BRICK_BORDER = 3                 # 테두리 두께 px (소수점이면 안쪽 반투명 선으로 안티앨리어싱)
BORDER_PX = math.ceil(BRICK_BORDER)  # 이웃 브릭과 겹치는 실제 픽셀 폭
LIST_PAD_X, LIST_GAP = 20, 0
DIM_OPACITY = 0.35
LIST_GLOSS_COLOR = "#888888"     # 리스트 행 오른쪽 한글 해석 (항상 고정 회색)
LIST_GLOSS_SIZE, LIST_GLOSS_MIN = 30, 20
LIST_GLOSS_GAP = 36              # 마지막 브릭과 해석 사이 간격
LIST_GLOSS_MIN_W = 360           # 해석용으로 항상 남겨둘 최소 폭

# 영문 글자 팝업: 브릭 테두리는 고정, 글자만 제자리에서 작게 -> 살짝 크게 -> 원래 크기
POP_FRAMES = 8                   # 8프레임 ≈ 0.27초
POP_START_SCALE = 0.5

# 화면 비율별 레이아웃 (모든 Y 좌표는 절대 고정 -> 한/영 전환 시 레이아웃 시프트 없음)
LAYOUTS = {
    "landscape": dict(
        W=1920, H=1080, MAX_LINE_W=1760, BRICK_FONT_SIZE=64, MIN_FONT_SIZE=48,
        PHASE_LABEL_Y=76, SPEAKER_Y=314, HINT_Y=697, MIC_Y=905, GUIDE_Y=990,
        LIST_TOP=170, LIST_BOTTOM=1020, LIST_ROW_MAX=120, LIST_FONT_SIZE=40, LIST_MIN_FONT=30,
        LIST_BRICK_H=62, LIST_SPEAKER_X=170, LIST_BRICK_X=260, LIST_RIGHT=80,
        TITLE_SIZE=110, PHASE_SIZE=84, OUTRO_SIZE=88, OUTRO_SUB=52,
    ),
    "portrait": dict(
        W=1080, H=1920, MAX_LINE_W=960, BRICK_FONT_SIZE=60, MIN_FONT_SIZE=44,
        PHASE_LABEL_Y=170, SPEAKER_Y=700, HINT_Y=1230, MIC_Y=1520, GUIDE_Y=1610,
        LIST_TOP=300, LIST_BOTTOM=1700, LIST_ROW_MAX=170, LIST_FONT_SIZE=38, LIST_MIN_FONT=24,
        LIST_BRICK_H=60, LIST_SPEAKER_X=80, LIST_BRICK_X=140, LIST_RIGHT=40,
        TITLE_SIZE=96, PHASE_SIZE=72, OUTRO_SIZE=62, OUTRO_SUB=40,
    ),
}


def apply_layout(name):
    """레이아웃 상수를 모듈 전역에 적용 (렌더링은 한 번에 하나씩만 실행)."""
    g = globals()
    g.update(LAYOUTS[name])
    g["LAYOUT"] = name
    g["CENTER_Y"] = g["H"] // 2                              # 영/한 자막 공통 절대 중심 Y
    g["LINE_H"] = g["BRICK_FONT_SIZE"] + 2 * BRICK_PAD_Y     # 영/한 공통 줄 높이


apply_layout("landscape")


# ───────────────────────── 데이터 ─────────────────────────
@dataclass
class Line:
    speaker: str
    bricks: list            # [(text, border_color, text_color)]
    ko: str
    en_text: str = ""
    en_audio: AudioSegment = None
    ko_audio: AudioSegment = None


@dataclass
class Chapter:
    name: str
    title: str
    lines: list = field(default_factory=list)


def clean_ko(text: str) -> str:
    """'(…)' 상황 연출 제거."""
    return re.sub(r"\s*\([^)]*\)", "", text).strip()


def read_rows(path: Path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    return [h.strip() for h in rows[0]], rows[1:]


def load_topic(path: Path) -> str:
    """'주제' 열의 첫 값, 없으면 파일명 마지막 '_' 뒤 (예: ..._아침깨우기 -> 아침깨우기)."""
    header, rows = read_rows(path)
    if "주제" in header:
        i = header.index("주제")
        for row in rows:
            if i < len(row) and row[i].strip():
                return row[i].strip()
    return Path(path).stem.split("_")[-1]


def load_csv(path: Path) -> list:
    header, rows = read_rows(path)

    def col(prefix):
        return next(i for i, h in enumerate(header) if h.startswith(prefix))

    i_age, i_spk, i_ko = col("연령대"), col("화자"), col("한글")
    brick_cols = sorted({(i, color, TEXT_EN) for p, color in BRICK_COLUMNS
                         for i, h in enumerate(header) if h.startswith(p)})

    chapters = {key: Chapter(key, title) for key, title in CHAPTERS}
    current = None
    for row in rows:
        if not any(c.strip() for c in row):
            continue
        row += [""] * (len(header) - len(row))
        age = row[i_age].strip()
        if age:
            current = next(k for k, _ in CHAPTERS if age.startswith(k))
        bricks = [(row[i].strip(), color, fg) for i, color, fg in brick_cols if row[i].strip()]
        if not bricks:
            continue
        speaker = row[i_spk].strip()
        if current == "영아" and speaker != "엄마":
            continue  # 영아 챕터는 엄마 혼잣말만
        line = Line(speaker, bricks, clean_ko(row[i_ko]))
        line.en_text = " ".join(t for t, _, _ in bricks)
        chapters[current].lines.append(line)
    return [c for c in chapters.values() if c.lines]


# ───────────────────────── TTS (Edge 전용) ─────────────────────────
def tts_path(text, voice):
    h = hashlib.md5(f"{voice}|{text}".encode()).hexdigest()[:16]
    return TTS_DIR / f"{h}.mp3"


async def synth(text, voice, sem, log):
    out = tts_path(text, voice)
    if out.exists() and out.stat().st_size > 0:
        return
    async with sem:
        for attempt in range(3):
            try:
                await edge_tts.Communicate(text, voice).save(str(out))
                return
            except Exception as e:
                if attempt == 2:
                    raise RuntimeError(f"TTS 실패 ({voice}): {text} / {e}") from e
                log(f"  TTS 재시도 ({e}): {text}")
                await asyncio.sleep(2)


def trim(seg: AudioSegment) -> AudioSegment:
    """앞뒤 무음 제거. 약한 첫 자음이 잘리지 않도록 앞쪽은 150ms 여유를 남김."""
    start = max(0, detect_leading_silence(seg, silence_threshold=-60) - 150)
    end = max(0, detect_leading_silence(seg.reverse(), silence_threshold=-50) - 80)
    return seg[start:len(seg) - end]


def prepare_audio(chapters, voices, log=print):
    """voices: {"en_mom": "en-US-...", ...}"""
    TTS_DIR.mkdir(parents=True, exist_ok=True)
    by_role = {role: voices.get(key) or DEFAULT_VOICES[key] for key, role in VOICE_ROLES.items()}
    jobs = set()
    for ch in chapters:
        for ln in ch.lines:
            jobs.add((ln.en_text, by_role[("en", ln.speaker)]))
            jobs.add((ln.ko, by_role[("ko", ln.speaker)]))

    async def run():
        sem = asyncio.Semaphore(4)
        await asyncio.gather(*(synth(t, v, sem, log) for t, v in jobs))

    log(f"[TTS] {len(jobs)}개 클립 생성/캐시 확인")
    asyncio.run(run())

    def load(text, voice):
        seg = AudioSegment.from_file(tts_path(text, voice))
        return trim(seg).set_frame_rate(SAMPLE_RATE).set_channels(1)

    for ch in chapters:
        for ln in ch.lines:
            ln.en_audio = load(ln.en_text, by_role[("en", ln.speaker)])
            ln.ko_audio = load(ln.ko, by_role[("ko", ln.speaker)])


# ───────────────────────── 프레임 렌더링 ─────────────────────────
_fonts = {}


def font(path, size):
    key = (path, size)
    if key not in _fonts:
        _fonts[key] = ImageFont.truetype(path, size)
    return _fonts[key]


def text_w(draw, text, fnt):
    return draw.textlength(text, font=fnt)


def wrap_rows(widths, max_w, gap):
    """폭 목록을 max_w 기준으로 줄 단위 인덱스 리스트로 나눔."""
    rows, cur, cur_w = [], [], 0
    for i, w in enumerate(widths):
        need = w if not cur else cur_w + gap + w
        if cur and need > max_w:
            rows.append(cur)
            cur, cur_w = [i], w
        else:
            cur.append(i)
            cur_w = need
    if cur:
        rows.append(cur)
    return rows


def block_top(n_rows):
    """줄 수와 무관하게 블록 중심을 CENTER_Y에 고정."""
    total = n_rows * LINE_H + (n_rows - 1) * LINE_GAP
    return CENTER_Y - total / 2


def fit_font(draw, texts, path, size, min_size, pad_x, gap, max_w):
    """모든 브릭이 max_w 안에 한 줄로 들어가는 가장 큰 글꼴과 각 브릭 폭."""
    for s in range(size, min_size - 1, -2):
        fnt = font(path, s)
        widths = [text_w(draw, t, fnt) + 2 * pad_x for t in texts]
        if sum(widths) + gap * (len(widths) - 1) <= max_w:
            break
    return fnt, widths


def draw_border(draw, box, color, width=BRICK_BORDER):
    """테두리만 그림. 정수 부분은 순색, 소수 부분은 그만큼 어둡게 한 안쪽 1px 선
    (검은 배경 위 부분 커버리지 = 안티앨리어싱된 소수점 두께)."""
    full = int(width)
    frac = width - full
    x0, y0, x1, y1 = box
    if full:
        draw.rectangle(box, outline=color, width=full)
    if frac > 0:
        r, g, b = ImageColor.getrgb(color)
        draw.rectangle([x0 + full, y0 + full, x1 - full, y1 - full],
                       outline=(round(r * frac), round(g * frac), round(b * frac)), width=1)


def draw_outline_row(draw, items, widths, x, y0, y1, fnt, pop=1.0, alpha=1.0):
    """테두리만 색칠한 브릭을 맞붙여 그림. 이웃 브릭이 경계선을 덮어써 선이 1줄로 보이게 함.
    pop/alpha는 글자에만 적용 (테두리 위치·크기는 고정)."""
    text_font = font(FONT_EN, max(1, round(fnt.size * pop)))
    level = round(255 * alpha)
    for (text, color, _), w in zip(items, widths):
        draw_border(draw, [x, y0, x + w, y1], color)
        if alpha > 0:
            draw.text((x + w / 2, (y0 + y1) / 2), text, font=text_font,
                      fill=(level, level, level), anchor="mm")
        x += w - BORDER_PX + BRICK_GAP


def draw_bricks(draw, bricks, pop=1.0, alpha=1.0):
    # 한 줄에 들어가도록 글자만 줄이고, 그래도 넘치면 줄바꿈 (블록 중심은 CENTER_Y 고정)
    fnt, widths = fit_font(draw, [t for t, _, _ in bricks], FONT_EN, BRICK_FONT_SIZE,
                           MIN_FONT_SIZE, BRICK_PAD_X, BRICK_GAP, MAX_LINE_W)
    rows = wrap_rows(widths, MAX_LINE_W, BRICK_GAP)
    y = block_top(len(rows))
    for row in rows:
        row_w = sum(widths[i] for i in row) - BORDER_PX * (len(row) - 1)
        draw_outline_row(draw, [bricks[i] for i in row], [widths[i] for i in row],
                         (W - row_w) / 2, y, y + LINE_H, fnt, pop, alpha)
        y += LINE_H + LINE_GAP


def draw_mic(draw, cx, cy):
    """안내 문구 위 마이크 아이콘."""
    draw.rounded_rectangle([cx - 24, cy - 50, cx + 24, cy + 10], 10, fill=MIC_COLOR)
    draw.rectangle([cx - 5, cy + 10, cx + 5, cy + 36], fill=MIC_COLOR)
    draw.rounded_rectangle([cx - 36, cy + 34, cx + 36, cy + 46], 3, fill=MIC_COLOR)


def draw_korean_center(draw, text):
    fnt = font(FONT_KO, BRICK_FONT_SIZE)
    words = text.split()
    widths = [text_w(draw, w, fnt) for w in words]
    space = text_w(draw, " ", fnt)
    rows = wrap_rows(widths, MAX_LINE_W, space)
    y = block_top(len(rows))
    for row in rows:
        s = " ".join(words[i] for i in row)
        draw.text((W / 2, y + LINE_H / 2), s, font=fnt, fill=KO_COLOR, anchor="mm")
        y += LINE_H + LINE_GAP


def wrap_text(draw, text, fnt, max_w):
    """단어 단위 줄바꿈. 한 단어가 max_w보다 길면 글자 단위로 자름."""
    lines, cur = [], ""
    for word in text.split():
        cand = f"{cur} {word}".strip()
        if text_w(draw, cand, fnt) <= max_w:
            cur = cand
            continue
        if cur:
            lines.append(cur)
        cur = ""
        for ch in word:
            if cur and text_w(draw, cur + ch, fnt) > max_w:
                lines.append(cur)
                cur = ""
            cur += ch
    if cur:
        lines.append(cur)
    return lines


def fit_gloss(draw, text, max_w, max_h):
    """남은 폭/행 높이 안에 들어가는 (글꼴, 줄 목록).
    한 줄 유지를 우선해 글꼴을 줄여보고, 그래도 안 되면 줄바꿈 (줄 수가 적은 쪽 우선)."""
    sizes = range(LIST_GLOSS_SIZE, LIST_GLOSS_MIN - 1, -2)
    for max_lines in (1, 2, 3):
        for s in sizes:
            fnt = font(FONT_KO_REG, s)
            lines = wrap_text(draw, text, fnt, max_w)
            if len(lines) <= max_lines and len(lines) * s * 1.25 <= max_h:
                return fnt, lines
    fnt = font(FONT_KO_REG, LIST_GLOSS_MIN)
    return fnt, wrap_text(draw, text, fnt, max_w)


def draw_dialogue_list(img, rows, active):
    """챕터 전체 대화를 고정 행 좌표에 나열. active 행만 100%, 나머지는 DIM_OPACITY.
    각 행 오른쪽 끝에 한글 해석을 회색으로 고정 표시 (하이라이트/딤 없음)."""
    n = len(rows)
    row_h = min(LIST_ROW_MAX, (LIST_BOTTOM - LIST_TOP) / n)
    top = (LIST_TOP + LIST_BOTTOM) / 2 - n * row_h / 2
    right = W - LIST_RIGHT
    # 해석 자리(LIST_GLOSS_MIN_W)를 남겨두고 브릭 폭을 맞춤
    max_w = right - LIST_BRICK_X - LIST_GLOSS_GAP - LIST_GLOSS_MIN_W
    base = ImageDraw.Draw(img)

    for i, (speaker, bricks, ko) in enumerate(rows):
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        cy = top + i * row_h + row_h / 2
        y0, y1 = cy - LIST_BRICK_H / 2, cy + LIST_BRICK_H / 2

        d.text((LIST_SPEAKER_X, cy), speaker, font=font(FONT_KO, 34), fill="#DADDE2", anchor="mm")
        texts = [t for t, _, _ in bricks]
        fnt, widths = fit_font(d, texts, FONT_EN, LIST_FONT_SIZE, LIST_MIN_FONT, LIST_PAD_X, LIST_GAP, max_w)
        if sum(widths) > max_w:  # 아주 긴 문장: 해석 자리를 지키도록 비상 축소
            fnt, widths = fit_font(d, texts, FONT_EN, LIST_MIN_FONT, 14, LIST_PAD_X // 2, LIST_GAP, max_w)
        draw_outline_row(d, bricks, widths, LIST_BRICK_X, y0, y1, fnt)

        if i != active:
            layer.putalpha(layer.getchannel("A").point(lambda a: int(a * DIM_OPACITY)))
        img.alpha_composite(layer)

        # 한글 해석: 마지막 브릭 오른쪽 남은 폭 안에서 줄바꿈/축소, 브릭과 세로 중앙 정렬
        if ko:
            gx = LIST_BRICK_X + sum(widths) - BORDER_PX * (len(widths) - 1) + LIST_GLOSS_GAP
            gfnt, lines = fit_gloss(base, ko, right - gx, row_h - 8)
            line_h = gfnt.size * 1.25
            gy = cy - line_h * (len(lines) - 1) / 2
            for k, line in enumerate(lines):
                base.text((gx, gy + k * line_h), line, font=gfnt, fill=LIST_GLOSS_COLOR, anchor="lm")


def draw_outro(d):
    line1, thumb = "오늘도 수고하셨습니다! ", "👍"
    line2 = "좋아요 & 구독 누르고 매일 함께 연습해요"
    f1, f2 = font(FONT_KO, OUTRO_SIZE), font(FONT_KO_REG, OUTRO_SUB)
    emoji = font(FONT_EMOJI, OUTRO_SIZE)

    w1, we = text_w(d, line1, f1), text_w(d, thumb, emoji)
    x = (W - w1 - we) / 2
    y1 = CENTER_Y - OUTRO_SIZE * 0.7
    d.text((x, y1), line1, font=f1, fill="#FFFFFF", anchor="lm")
    d.text((x + w1, y1), thumb, font=emoji, embedded_color=True, anchor="lm")
    d.text((W / 2, CENTER_Y + OUTRO_SIZE * 0.8), line2, font=f2, fill="#ECCC68", anchor="mm")


def render_frame(spec: dict) -> Path:
    """spec(+레이아웃)을 해시해 같은 화면은 한 번만 렌더링."""
    key = hashlib.md5(repr((LAYOUT, BRICK_BORDER, sorted(spec.items()))).encode()).hexdigest()[:16]
    out = FRAME_DIR / f"{key}.png"
    if out.exists():
        return out

    kind = spec["kind"]
    img = Image.new("RGBA", (W, H), TITLE_BG if kind == "title" else BG)
    d = ImageDraw.Draw(img)

    if kind == "title":
        d.text((W / 2, CENTER_Y - 40), spec["title"], font=font(FONT_KO, TITLE_SIZE), fill="#FFFFFF", anchor="mm")
        d.text((W / 2, CENTER_Y + 80), spec.get("sub", ""), font=font(FONT_KO_REG, 44), fill=LABEL_COLOR, anchor="mm")
    elif kind == "phase":
        d.text((W / 2, CENTER_Y - 30), spec["title"], font=font(FONT_KO, PHASE_SIZE), fill="#FFFFFF", anchor="mm")
        d.text((W / 2, CENTER_Y + 60), spec["sub"], font=font(FONT_KO_REG, 40), fill=LABEL_COLOR, anchor="mm")
    elif kind == "outro":
        draw_outro(d)
    elif kind == "intro":
        from thumbnail import render_thumbnail  # 순환 import 방지
        img = render_thumbnail(spec["title"]).convert("RGBA")
    else:
        # 상단 라벨/화자/가이드는 모두 고정 좌표
        d.text((W / 2, PHASE_LABEL_Y), spec["label"], font=font(FONT_KO_REG, 34), fill=LABEL_COLOR, anchor="mm")
        if kind == "list":
            draw_dialogue_list(img, spec["rows"], spec["active"])
        else:
            if spec.get("speaker"):
                d.text((W / 2, SPEAKER_Y), spec["speaker"], font=font(FONT_KO, 40), fill="#DADDE2", anchor="mm")
            if kind == "en":
                draw_bricks(d, spec["bricks"], spec.get("pop", 1.0), spec.get("alpha", 1.0))
                if spec.get("hint"):
                    d.text((W / 2, HINT_Y), spec["hint"], font=font(FONT_KO_REG, 40), fill=KO_COLOR, anchor="mm")
            elif kind == "ko":
                draw_korean_center(d, spec["text"])
            if spec.get("guide"):
                draw_mic(d, W / 2, MIC_Y)
                d.text((W / 2, GUIDE_Y), spec["guide"], font=font(FONT_KO_REG, 36), fill="#FFB020", anchor="mm")

    img.convert("RGB").save(out)
    return out


# ───────────────────────── 타임라인 ─────────────────────────
_tone_bank = None


def room_tone(ms: int) -> AudioSegment:
    """무음 구간용 초저레벨 잡음 (ROOM_TONE_DBFS).
    완전한 디지털 무음(0)이 이어지면 블루투스/HDMI 등 재생 장치가 절전에 들어가
    다음 음성의 첫 음절을 먹어버리므로, 귀에 들리지 않는 신호로 출력을 계속 유지."""
    global _tone_bank
    if _tone_bank is None:
        rng = np.random.default_rng(7)
        n = SAMPLE_RATE * 10
        noise = np.convolve(rng.standard_normal(n), np.ones(8) / 8, mode="same")  # 고역 완화
        noise *= 32767 * 10 ** (ROOM_TONE_DBFS / 20) / noise.std()
        _tone_bank = AudioSegment(noise.astype(np.int16).tobytes(), frame_rate=SAMPLE_RATE,
                                  sample_width=2, channels=1)
    out = AudioSegment.empty()
    while len(out) < ms:
        out += _tone_bank[:ms - len(out)]
    return out.set_frame_rate(SAMPLE_RATE)


def ease_out_back(t, c1=1.70158):
    """0->1, 끝에서 살짝 넘쳤다가(약 10%) 돌아오는 기본 팝업 곡선."""
    return 1 + (c1 + 1) * (t - 1) ** 3 + c1 * (t - 1) ** 2


class Timeline:
    def __init__(self):
        self.audio = AudioSegment.silent(0, frame_rate=SAMPLE_RATE)
        self.shots = []  # (frame_path, start_ms)
        self.hold_until = 0  # 이 시점 전에는 화면을 바꾸지 않음
        self.markers = []    # (라벨, 시작 ms) — 유튜브 타임라인 챕터용

    def mark(self, label):
        """직전에 띄운 화면이 실제로 시작되는 시각을 챕터로 기록."""
        self.markers.append((label, self.shots[-1][1]))

    def show(self, spec, pop=False):
        """pop=True면 영문 글자 팝업 프레임(POP_FRAMES개)을 먼저 깔고 최종 화면으로 고정.
        뒤따르는 play()의 1초 패딩 안에서 끝나므로 다른 컷과 겹치지 않음."""
        frame = render_frame(spec)
        if self.shots and self.shots[-1][0] == frame:
            return
        if len(self.audio) < self.hold_until:  # 음성 직후라면 남은 유지 시간만큼 무음 후 전환
            self.silence(self.hold_until - len(self.audio))
        t0 = len(self.audio)
        if pop:
            for k in range(POP_FRAMES):
                t = (k + 1) / POP_FRAMES
                scale = POP_START_SCALE + (1 - POP_START_SCALE) * ease_out_back(t)
                pop_spec = {**spec, "pop": round(scale, 3), "alpha": round(min(1.0, t * 2), 3)}
                self.shots.append((render_frame(pop_spec), t0 + k * 1000 / FPS))
            t0 += POP_FRAMES * 1000 / FPS
        self.shots.append((frame, t0))

    def play(self, seg, on_voice=None):
        """[LEAD_PAD_MS 무음] + 음성. on_voice 화면은 실제 음성 시작 시점에 전환."""
        self.silence(LEAD_PAD_MS)
        if on_voice:
            self.show(on_voice)
        self.audio += seg
        self.hold_until = len(self.audio) + HOLD_AFTER_VOICE_MS

    def silence(self, ms):
        self.audio += room_tone(int(ms))

    def wait_after(self, seg, guide_spec):
        """섀도잉 대기: 직전 음성(패딩 제외) 길이 x WAIT_FACTOR.
        유지 시간도 대기에 포함되며, 안내 화면은 유지 시간이 지난 뒤 표시."""
        total = len(seg) * WAIT_FACTOR
        hold = min(HOLD_AFTER_VOICE_MS, total)
        self.silence(hold)
        self.show(guide_spec)
        self.silence(total - hold)


def en_spec(label, ln, guide=None):
    return {"kind": "en", "label": label, "speaker": ln.speaker,
            "bricks": tuple(ln.bricks), "hint": ln.ko, "guide": guide}


def build_timeline(chapters, topic, intro_audio=None) -> Timeline:
    """intro_audio가 있으면 맨 앞에 인트로(섬네일 화면 + 안내 음성, 최소 INTRO_MIN_MS)를 넣음.
    유튜브 타임라인 챕터는 첫 항목 00:00과 각 구간 10초 이상이 필요해서 롱폼에만 사용."""
    tl = Timeline()

    if intro_audio is not None:
        tl.show({"kind": "intro", "title": topic})
        tl.mark("인트로")
        tl.play(intro_audio)
        tl.silence(max(0, INTRO_MIN_MS - len(tl.audio)))

    def phase_card(title, sub):
        if PHASE_CARD_MS:
            tl.show({"kind": "phase", "title": title, "sub": sub})
            tl.silence(PHASE_CARD_MS)

    # 전체 대화문 리스트 화면은 16:9 롱폼에서만 사용.
    # 9:16 숏폼은 모바일 가독성과 길이를 위해 Phase 1을 건너뛰고, Phase 4도 개별 문장 화면으로 진행.
    overview = LAYOUT == "landscape"

    def listen_pass(ch, label):
        if not overview:
            for ln in ch.lines:
                tl.show(en_spec(label, ln), pop=True)
                tl.play(ln.en_audio)
            return

        rows = tuple((ln.speaker, tuple(ln.bricks), ln.ko) for ln in ch.lines)

        def list_spec(active):
            return {"kind": "list", "label": label, "rows": rows, "active": active}

        tl.show(list_spec(-1))
        for i, ln in enumerate(ch.lines):
            tl.play(ln.en_audio, on_voice=list_spec(i))   # 음성 시작부터 하이라이트
            tl.show(list_spec(-1))                        # 음성 종료 1초 뒤 딤 처리

    for ch in chapters:
        tl.show({"kind": "title", "title": ch.title, "sub": f"{topic} · 영어 섀도잉"})
        tl.mark(f"{ch.title} 표현")
        tl.silence(CHAPTER_TITLE_MS)

        tag = f"{ch.title}  |  "

        if overview:
            phase_card("Phase 1 · 듣기", "대화를 편하게 들어보세요")
            listen_pass(ch, tag + "Phase 1 · 듣기")

        def counter(r):
            return f" ({r}/{ROUNDS})" if ROUNDS > 1 else ""

        phase_card("Phase 2 · 따라 말하기", f"들은 문장을 그대로 따라 말해보세요 (문장당 {ROUNDS}회)")
        for ln in ch.lines:
            for r in range(1, ROUNDS + 1):
                label = tag + "Phase 2 · 따라 말하기" + counter(r)
                tl.show(en_spec(label, ln), pop=(r == 1))   # 문장이 처음 나타날 때만 팝업
                tl.play(ln.en_audio)
                tl.wait_after(ln.en_audio, en_spec(label, ln, guide="따라 말해보세요"))

        phase_card("Phase 3 · 영작하기", f"한국어를 듣고 영어로 말해보세요 (문장당 {ROUNDS}회)")
        for ln in ch.lines:
            for r in range(1, ROUNDS + 1):
                label = tag + "Phase 3 · 영작하기" + counter(r)
                ko_spec = {"kind": "ko", "label": label, "speaker": ln.speaker, "text": ln.ko}
                tl.show(ko_spec)
                tl.play(ln.ko_audio)
                tl.wait_after(ln.ko_audio, {**ko_spec, "guide": "영어로 말해보세요"})
                tl.show(en_spec(label, ln), pop=True)
                tl.play(ln.en_audio)

        phase_card("Phase 4 · 동시 섀도잉", "원어민 음성과 동시에 말해보세요")
        listen_pass(ch, tag + "Phase 4 · 동시 섀도잉")
        tl.silence(1000)

    tl.show({"kind": "outro"})
    tl.silence(OUTRO_MS)
    return tl


# ───────────────────────── 인코딩 ─────────────────────────
def encode(tl: Timeline, out_mp4: Path, log=print, progress=None):
    """progress(0.0~1.0)로 ffmpeg 진행률 보고."""
    BUILD.mkdir(parents=True, exist_ok=True)
    wav = BUILD / "timeline.wav"
    tl.audio.export(wav, format="wav")
    total_ms = len(tl.audio)

    # 각 컷 경계를 프레임 단위로 반올림해 누적 드리프트 방지
    def to_frame(ms):
        return round(ms * FPS / 1000)

    bounds = [to_frame(s) for _, s in tl.shots] + [to_frame(total_ms)]
    concat = BUILD / "frames.txt"
    with open(concat, "w", encoding="utf-8") as f:
        for (frame, _), a, b in zip(tl.shots, bounds, bounds[1:]):
            if b <= a:
                continue
            f.write(f"file '{frame.as_posix()}'\nduration {(b - a) / FPS:.6f}\n")
        f.write(f"file '{tl.shots[-1][0].as_posix()}'\n")

    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1",
        "-f", "concat", "-safe", "0", "-i", str(concat),
        "-i", str(wav),
        "-vf", f"fps={FPS},format=yuv420p", "-fps_mode", "cfr",
        "-c:v", "libx264", "-preset", "medium", "-tune", "stillimage", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-t", f"{total_ms / 1000:.3f}", "-movflags", "+faststart",
        str(out_mp4),
    ]
    log(f"[ffmpeg] {len(tl.shots)}컷, {total_ms / 1000:.1f}초 인코딩")
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", creationflags=flags)
    for line in proc.stdout:
        if line.startswith("out_time_us=") and progress:
            try:
                progress(min(1.0, int(line.split("=")[1]) / 1000 / total_ms))
            except ValueError:
                pass
    err = proc.stderr.read()
    if proc.wait() != 0:
        raise RuntimeError(f"ffmpeg 실패: {err.strip()[-500:]}")
    return total_ms / 1000


# ───────────────────────── 고수준 API (GUI/CLI 공용) ─────────────────────────
def _render_one(chapters, topic, out_mp4, log, progress, intro_audio=None):
    """progress: 0~1 구간 콜백. 프레임 구성 0~0.15, 인코딩 0.15~1. 반환: (길이 초, 타임라인)."""
    FRAME_DIR.mkdir(parents=True, exist_ok=True)
    log(f"[화면] {LAYOUT} 프레임 구성")
    tl = build_timeline(chapters, topic, intro_audio)
    progress(0.15)
    sec = encode(tl, out_mp4, log, lambda f: progress(0.15 + 0.85 * f))
    progress(1.0)
    return sec, tl


# ───────────────────────── 유튜브 메타데이터 ─────────────────────────
META_TAGS = ["육아영어", "영어섀도잉", "조립식영어", "엄마표영어", "영어회화"]
META_HASHTAGS = " ".join(f"#{t}" for t in META_TAGS)
META_INTRO = ("매일 반복하는 조립식 육아영어 스피킹 연습!\n"
              "청크(브릭)를 결합하여 자연스러운 영어 문장을 완성해 보세요.")
LONG_TITLE = "[{topic}] 하루 15분 육아영어 스피킹 연습 (영아/유아/초등)"
SHORT_TITLE = "[{topic}] {age} {minutes}분 육아영어 섀도잉 #Shorts"
YT_CHAPTER_MIN_SEC = 10


def fmt_ts(sec: float) -> str:
    """유튜브 설명란 타임스탬프 (mm:ss, 1시간 이상이면 h:mm:ss). 초 단위 내림."""
    s = int(sec)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def check_chapters(markers, total_ms, log):
    """유튜브 챕터 인식 조건: 00:00 시작, 3개 이상, 각 구간 10초 이상."""
    starts = [int(ms // 1000) for _, ms in markers] + [int(total_ms // 1000)]
    problems = []
    if not markers or starts[0] != 0:
        problems.append("첫 챕터가 00:00이 아님")
    if len(markers) < 3:
        problems.append("챕터가 3개 미만")
    if any(b - a < YT_CHAPTER_MIN_SEC for a, b in zip(starts, starts[1:])):
        problems.append("10초 미만 구간 있음")
    if problems:
        log(f"  [주의] 유튜브 챕터로 인식되지 않을 수 있음: {', '.join(problems)}")


def long_meta(topic, tl, sec, thumbnail_path):
    chapters = "\n".join(f"{fmt_ts(ms / 1000)} {label}" for label, ms in tl.markers)
    return {
        "kind": "long",
        "title": LONG_TITLE.format(topic=topic),
        "description": f"{META_INTRO}\n\n[타임라인 챕터]\n{chapters}\n\n{META_HASHTAGS}",
        "tags": META_TAGS + [topic, "스피킹연습"],
        "thumbnail": str(thumbnail_path),
        "chapters": [{"time": fmt_ts(ms / 1000), "title": label} for label, ms in tl.markers],
        "duration_sec": round(sec, 1),
    }


def short_meta(topic, ch, sec):
    age = ch.title.split(" (")[0]                     # "초등 이상 (8세~)" -> "초등 이상"
    minutes = 3 if sec <= SHORTS_MAX_SEC else math.ceil(sec / 60)
    return {
        "kind": "short",
        "title": SHORT_TITLE.format(topic=topic, age=age, minutes=minutes),
        "description": f"{META_INTRO}\n\n[{topic}] {ch.title} 표현\n\n{META_HASHTAGS} #Shorts",
        "tags": ["Shorts"] + META_TAGS + [topic, age],
        "duration_sec": round(sec, 1),
    }


def write_meta(mp4: Path, meta: dict):
    """영상과 같은 이름의 .json. 렌더링마다 자동 생성/갱신되고, 업로드 시 그대로 사용."""
    mp4.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def tts_clip(text, voice, log=print) -> AudioSegment:
    TTS_DIR.mkdir(parents=True, exist_ok=True)
    asyncio.run(synth(text, voice, asyncio.Semaphore(1), log))
    return trim(AudioSegment.from_file(tts_path(text, voice))).set_frame_rate(SAMPLE_RATE).set_channels(1)


def render_long(csv_path, out_dir, voices=None, log=print, progress=lambda f: None) -> Path:
    csv_path, out_dir = Path(csv_path), Path(out_dir)
    voices = voices or DEFAULT_VOICES
    topic = load_topic(csv_path)
    chapters = load_csv(csv_path)
    for ch in chapters:
        log(f"[CSV] {ch.title}: {len(ch.lines)}문장")
    prepare_audio(chapters, voices, log)
    intro = tts_clip(INTRO_TEXT.format(topic=topic), voices.get("ko_mom") or DEFAULT_VOICES["ko_mom"], log)
    progress(0.1)

    apply_layout("landscape")
    apply_timing("full")
    out = out_dir / f"{topic}_롱폼_16x9.mp4"
    sec, tl = _render_one(chapters, topic, out, log, lambda f: progress(0.1 + 0.9 * f), intro)
    meta = long_meta(topic, tl, sec, out_dir / f"{topic}_섬네일.png")
    check_chapters(tl.markers, len(tl.audio), log)
    write_meta(out, meta)
    log("[메타데이터] " + meta["title"])
    for c in meta["chapters"]:
        log(f"  {c['time']} {c['title']}")
    log(f"[완료] {out} ({sec / 60:.1f}분)")
    return out


def fit_compact_wait(chapters, topic, log=print):
    """압축 모드에서 COMPACT_TARGET_SEC 안에 들어오는 가장 큰 대기 배수(2.0 ~ COMPACT_MIN_WAIT)를 적용.
    영상 길이는 대기 배수에 정비례하므로 두 점으로 선형 보간."""
    def length(factor):
        apply_timing("compact", WAIT_FACTOR=factor)
        return len(build_timeline(chapters, topic).audio) / 1000

    full = TIMING_PROFILES["compact"]["WAIT_FACTOR"]
    d_full = length(full)
    if d_full <= COMPACT_TARGET_SEC:
        apply_timing("compact")
        return
    d_min = length(COMPACT_MIN_WAIT)
    factor = full - (d_full - COMPACT_TARGET_SEC) / (d_full - d_min) * (full - COMPACT_MIN_WAIT)
    factor = max(COMPACT_MIN_WAIT, math.floor(factor * 100) / 100)
    apply_timing("compact", WAIT_FACTOR=factor)
    log(f"  압축: 대기 배수 {full} -> {factor} (예상 {length(factor):.0f}초)")
    if factor == COMPACT_MIN_WAIT and d_min > COMPACT_TARGET_SEC:
        log(f"  [주의] 최소 대기 배수로도 {d_min:.0f}초 — 문장 수를 줄여야 3분 안에 들어옵니다.")


def render_shorts(csv_path, out_dir, voices=None, log=print, progress=lambda f: None,
                  compact=False) -> list:
    """compact=True: YouTube Shorts(3분 이하)용 압축 타이밍."""
    csv_path, out_dir = Path(csv_path), Path(out_dir)
    topic = load_topic(csv_path)
    chapters = load_csv(csv_path)
    prepare_audio(chapters, voices or DEFAULT_VOICES, log)
    progress(0.1)

    apply_layout("portrait")
    outs = []
    try:
        for n, ch in enumerate(chapters):
            out = out_dir / f"{topic}_숏폼_{n + 1}_{ch.name}_9x16.mp4"
            log(f"[숏폼 {n + 1}/{len(chapters)}] {ch.title}{' (3분 압축)' if compact else ''}")
            if compact:
                fit_compact_wait([ch], topic, log)
            span = 0.9 / len(chapters)
            sec, _ = _render_one([ch], topic, out, log,
                                 lambda f, base=0.1 + n * span: progress(base + span * f))
            meta = short_meta(topic, ch, sec)
            write_meta(out, meta)
            log("[메타데이터] " + meta["title"])
            if sec > SHORTS_MAX_SEC:
                log(f"  [주의] {sec:.0f}초 -> YouTube Shorts 상한({SHORTS_MAX_SEC}초)을 넘어 일반 영상으로 분류될 수 있음")
            log(f"[완료] {out} ({sec:.0f}초)")
            outs.append(out)
    finally:
        apply_layout("landscape")
        apply_timing("full")
    return outs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv", nargs="?", default=str(DEFAULT_CSV))
    ap.add_argument("--format", choices=["long", "shorts"], default="long")
    ap.add_argument("--compact", action="store_true", help="숏폼 3분 압축 모드")
    ap.add_argument("-o", "--out-dir", default=str(DEFAULT_OUT))
    args = ap.parse_args()
    if args.format == "long":
        render_long(args.csv, args.out_dir)
    else:
        render_shorts(args.csv, args.out_dir, compact=args.compact)


if __name__ == "__main__":
    sys.exit(main())
