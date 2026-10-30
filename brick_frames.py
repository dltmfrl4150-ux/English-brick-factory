"""
브릭 학습 모드 — 화면(프레임) 그리기

화면 시안 기준: 배경 #2C2C2C, 블록 안 글자 흰색, 둥근 모서리 색 블록, 블록 아래 뜻.
레이아웃은 블록이 공개되는 단계와 무관하게 항상 같은 위치·크기 (공개 시 화면 흔들림 없음).

render(spec, layout) -> PIL.Image
  spec["kind"]: intro | learn | title | review | outro
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# ───────── 색 ─────────
BG = "#2C2C2C"
BLOCK_TEXT = "#FFFFFF"
LABEL = "#AEAEAE"        # 상단 진행 라벨
SPEAKER = "#7F7F7F"
SENTENCE = "#D9D9D9"     # 전체 번역 / 블록 아래 뜻
MUTED = "#8A8A8A"        # 카운트다운
MIC = "#747474"
GUIDE = "#BDBDBD"
ACCENT = "#A1281F"       # 아웃트로 구독 버튼 (RED 기본색)

# ───────── 글꼴 (Windows 맑은 고딕 우선, 없으면 Noto Sans CJK KR) ─────────
_BOLD = [("C:/Windows/Fonts/malgunbd.ttf", 0), ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 1)]
_REG = [("C:/Windows/Fonts/malgun.ttf", 0), ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 1)]
_cache = {}


def _font(cands, size):
    key = (id(cands), size)
    if key not in _cache:
        for path, idx in cands:
            if Path(path).exists():
                _cache[key] = ImageFont.truetype(path, size, index=idx)
                break
        else:
            raise FileNotFoundError("한글 글꼴을 찾을 수 없습니다 (맑은 고딕 / Noto Sans CJK).")
    return _cache[key]


def bold(size):
    return _font(_BOLD, size)


def reg(size):
    return _font(_REG, size)


# ───────── 화면 문구 (학습 언어별) ─────────
UI = {
    "en": dict(guide="영어로 말해보세요", learn="블록학습", review="이어서 말해보기",
               target="대상", situation="상황", outro1="구독과 좋아요 누르고", outro2="같이 매일 연습해요!",
               like="좋아요", sub="구독", subbed="구독중"),
    "ko": dict(guide="Say it in Korean", learn="Build the sentence", review="Say it all",
               target="Target", situation="Situation", outro1="Like & subscribe", outro2="and practice with us every day!",
               like="Like", sub="Subscribe", subbed="Subscribed"),
}

# 화자 / 그룹 표시 이름 (CSV 값 -> 화면). 없는 값은 CSV 값 그대로 표시
NAMES = {
    "en": {"엄마": "엄마", "아이": "아이", "아빠": "아빠",
           "영아": "영아 (0~3세)", "유아": "유아 (4~7세)", "초등": "초등 이상 (8세~)"},
    "ko": {"엄마": "Mom", "아이": "Child", "아빠": "Dad",
           "영아": "Babies (0-3)", "유아": "Preschoolers (4-7)", "초등": "School-age (8+)"},
}


# 화면에 표시하는 단계 번호 (STEP 1 블록학습, STEP 2 이어서 말해보기)
STEPS = {"learn": 1, "review": 2}


def step_text(lang, kind):
    return f"STEP {STEPS[kind]} {UI[lang][kind]}"


def name(lang, value):
    return NAMES.get(lang, {}).get(value, value)


# ───────── 레이아웃 (모든 좌표 고정) ─────────
# 9:16은 YouTube Shorts / 릴스의 우측 버튼·하단 제목 영역을 피해서 중앙 쪽에 배치
LAYOUTS = {
    "landscape": dict(
        W=1920, H=1080, LABEL_Y=67, LABEL_SIZE=30, SPEAKER_Y=160, SPEAKER_SIZE=34,
        TRANS_Y=258, TRANS_SIZE=48, TRANS_MAX_W=1600,
        BLOCK_CY=560, BLOCK_H=120, BLOCK_FONT=60, BLOCK_MIN_FONT=44, PAD_X=30, GAP_X=16, RADIUS=8,
        MEAN_SIZE=44, MEAN_OFFSET=62, ROW_H=230, MAX_W=1760,
        COUNT_POS=(1810, 990), COUNT_SIZE=72, MIC_Y=880, GUIDE_Y=975, GUIDE_SIZE=36,
        TITLE_SIZE=120, INFO_SIZE=42, OUTRO_SIZE=84,
    ),
    "portrait": dict(
        W=1080, H=1920, LABEL_Y=200, LABEL_SIZE=30, SPEAKER_Y=330, SPEAKER_SIZE=36,
        TRANS_Y=450, TRANS_SIZE=48, TRANS_MAX_W=940,
        BLOCK_CY=860, BLOCK_H=104, BLOCK_FONT=52, BLOCK_MIN_FONT=40, PAD_X=24, GAP_X=14, RADIUS=8,
        MEAN_SIZE=38, MEAN_OFFSET=54, ROW_H=240, MAX_W=980,
        COUNT_POS=(540, 1260), COUNT_SIZE=80, MIC_Y=1390, GUIDE_Y=1480, GUIDE_SIZE=36,
        TITLE_SIZE=96, INFO_SIZE=38, OUTRO_SIZE=64,
    ),
}


def _tw(d, text, fnt):
    return d.textlength(text, font=fnt)


def _wrap_words(d, text, fnt, max_w):
    lines, cur = [], ""
    for w in text.split():
        cand = f"{cur} {w}".strip()
        if cur and _tw(d, cand, fnt) > max_w:
            lines.append(cur)
            cur = w
        else:
            cur = cand
    return lines + ([cur] if cur else [])


def block_layout(d, blocks, L):
    """블록 위치 계산. blocks: [(text, meaning, color)].
    폭 = max(표현, 뜻) + 여백. 한 줄에 들어가면 글자를 줄이고(최소 크기까지), 그래도 넘치면 줄바꿈.
    반환: (블록 글꼴, 뜻 글꼴, [(x0, y0, x1, y1)])"""
    def widths(size):
        bf, mf = bold(size), bold(round(L["MEAN_SIZE"] * size / L["BLOCK_FONT"]))
        return bf, mf, [max(_tw(d, t, bf), _tw(d, m, mf)) + 2 * L["PAD_X"] for t, m, _ in blocks]

    gap = L["GAP_X"]
    for size in range(L["BLOCK_FONT"], L["BLOCK_MIN_FONT"] - 1, -2):
        bf, mf, ws = widths(size)
        if sum(ws) + gap * (len(ws) - 1) <= L["MAX_W"]:
            rows = [list(range(len(ws)))]
            break
    else:  # 줄바꿈 (기본 크기)
        bf, mf, ws = widths(L["BLOCK_FONT"])
        rows, cur, cur_w = [], [], 0
        for i, w in enumerate(ws):
            need = w if not cur else cur_w + gap + w
            if cur and need > L["MAX_W"]:
                rows.append(cur)
                cur, cur_w = [i], w
            else:
                cur.append(i)
                cur_w = need
        rows.append(cur)

    boxes = [None] * len(blocks)
    top_cy = L["BLOCK_CY"] - (len(rows) - 1) * L["ROW_H"] / 2
    for r, row in enumerate(rows):
        cy = top_cy + r * L["ROW_H"]
        row_w = sum(ws[i] for i in row) + gap * (len(row) - 1)
        x = (L["W"] - row_w) / 2
        for i in row:
            boxes[i] = (x, cy - L["BLOCK_H"] / 2, x + ws[i], cy + L["BLOCK_H"] / 2)
            x += ws[i] + gap
    return bf, mf, boxes


MIC_IMAGE = Path(__file__).resolve().parent / "assets" / "mic.png"
MIC_H = 96               # 마이크 아이콘 높이 px (이전 도형 마이크와 비슷한 크기)
_mic = None


def _draw_mic(img, cx, cy):
    """assets/mic.png (투명 배경)를 (cx, cy) 중심에 붙임."""
    global _mic
    if _mic is None:
        src = Image.open(MIC_IMAGE).convert("RGBA")
        _mic = src.resize((round(src.width * MIC_H / src.height), MIC_H), Image.LANCZOS)
    img.paste(_mic, (round(cx - _mic.width / 2), round(cy - 4 - MIC_H / 2)), _mic)


def _draw_countdown(d, L, n):
    cx, cy = L["COUNT_POS"]
    r = L["COUNT_SIZE"] * 0.75
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=MUTED, width=4)
    d.text((cx, cy), str(n), font=bold(L["COUNT_SIZE"]), fill=MUTED, anchor="mm")


def _draw_header(d, L, spec, step):
    """상단 소제목: '학습종류 | 주제 STEP N 단계명' (학습종류가 없으면 '주제 STEP N 단계명')."""
    lf = reg(L["LABEL_SIZE"])
    head = f"{spec['learn_type']}  |  {spec['topic']}" if spec.get("learn_type") else spec["topic"]
    tail = step
    one = f"{head}  {tail}"
    if _tw(d, one, lf) <= L["W"] - 80:
        d.text((L["W"] / 2, L["LABEL_Y"]), one, font=lf, fill=LABEL, anchor="mm")
    else:  # 좁은 화면: 주제 / 진행 단계 두 줄
        lh = L["LABEL_SIZE"] * 1.4
        d.text((L["W"] / 2, L["LABEL_Y"] - lh / 2), head, font=lf, fill=LABEL, anchor="mm")
        d.text((L["W"] / 2, L["LABEL_Y"] + lh / 2), tail, font=lf, fill=LABEL, anchor="mm")
    d.text((L["W"] / 2, L["SPEAKER_Y"]), name(spec["lang"], spec["speaker"]), font=bold(L["SPEAKER_SIZE"]), fill=SPEAKER, anchor="mm")
    fnt = bold(L["TRANS_SIZE"])
    lines = _wrap_words(d, spec["translation"], fnt, L["TRANS_MAX_W"])
    lh = L["TRANS_SIZE"] * 1.35
    for k, line in enumerate(lines):
        d.text((L["W"] / 2, L["TRANS_Y"] + (k - (len(lines) - 1) / 2) * lh), line, font=fnt, fill=SENTENCE, anchor="mm")


def _draw_learn(img, d, L, spec):
    ui = UI[spec["lang"]]
    _draw_header(d, L, spec, step_text(spec["lang"], spec["kind"]))
    blocks = spec["blocks"]
    bf, mf, boxes = block_layout(d, blocks, L)
    revealed, meanings, pop = spec.get("revealed", 0), spec.get("meanings", 0), spec.get("pop", 1.0)
    for i, ((text, meaning, color), box) in enumerate(zip(blocks, boxes)):
        d.rounded_rectangle(box, L["RADIUS"], fill=color)
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        if i < revealed:
            # 방금 공개된 블록만 팝업 크기 적용
            f = bold(max(1, round(bf.size * pop))) if i == revealed - 1 and pop != 1.0 else bf
            d.text((cx, cy), text, font=f, fill=BLOCK_TEXT, anchor="mm")
        if i < meanings:
            d.text((cx, box[3] + L["MEAN_OFFSET"]), meaning, font=mf, fill=SENTENCE, anchor="mm")
    if spec.get("countdown"):
        _draw_countdown(d, L, spec["countdown"])
    if spec.get("guide"):
        _draw_mic(img, L["W"] / 2, L["MIC_Y"])
        d.text((L["W"] / 2, L["GUIDE_Y"]), ui["guide"], font=reg(L["GUIDE_SIZE"]), fill=GUIDE, anchor="mm")


def _fit(d, text, cands_size, max_w, min_size):
    size = cands_size
    while size > min_size and _tw(d, text, bold(size)) > max_w:
        size -= 4
    return bold(size)


def _draw_intro(d, L, spec):
    ui = UI[spec["lang"]]
    W, H = L["W"], L["H"]
    top = H * (0.36 if W > H else 0.33)
    d.text((W / 2, top), spec["topic"], font=_fit(d, spec["topic"], L["TITLE_SIZE"], W - 160, 48),
           fill="#FFFFFF", anchor="mm")
    # 그룹 캡슐
    group = name(spec["lang"], spec["group"])
    gf = bold(L["INFO_SIZE"])
    gw = _tw(d, group, gf) + 80
    gy = top + L["TITLE_SIZE"] * 1.0
    d.rounded_rectangle([(W - gw) / 2, gy - 36, (W + gw) / 2, gy + 36], 36, outline="#FFFFFF", width=2)
    d.text((W / 2, gy), group, font=gf, fill="#FFFFFF", anchor="mm")
    # 대상 / 상황
    f = reg(L["INFO_SIZE"])
    y = gy + 120
    for label, value in ((ui["target"], spec["target"]), (ui["situation"], spec["situation"])):
        for line in _wrap_words(d, f"{label}: {value}", f, W - 200):
            d.text((W / 2, y), line, font=f, fill=SENTENCE, anchor="mm")
            y += L["INFO_SIZE"] * 1.5


def _draw_title(d, L, spec):
    """단계 제목 카드: 'STEP N' + 단계명 (예: STEP 2 / 이어서 말해보기)."""
    W, H = L["W"], L["H"]
    ui = UI[spec["lang"]]
    cy = H * (0.45 if W > H else 0.40)
    d.text((W / 2, cy - L["TITLE_SIZE"] * 0.75), f"STEP {STEPS[spec['step']]}", font=bold(L["OUTRO_SIZE"]),
           fill=LABEL, anchor="mm")
    label = ui[spec["step"]]
    d.text((W / 2, cy + L["TITLE_SIZE"] * 0.5), label, font=_fit(d, label, L["TITLE_SIZE"], W - 160, 48),
           fill="#FFFFFF", anchor="mm")


def _draw_outro(d, L, spec):
    """t: 0~1 애니메이션 진행. 0~0.3 문구, 0.3~0.6 버튼 등장, 0.6~ 좋아요·구독 눌림."""
    ui = UI[spec["lang"]]
    W, H = L["W"], L["H"]
    t = spec.get("t", 1.0)
    cy = H * (0.42 if W > H else 0.40)
    f = bold(L["OUTRO_SIZE"])
    d.text((W / 2, cy - L["OUTRO_SIZE"] * 0.7), ui["outro1"], font=_fit(d, ui["outro1"], L["OUTRO_SIZE"], W - 120, 36),
           fill="#FFFFFF", anchor="mm")
    d.text((W / 2, cy + L["OUTRO_SIZE"] * 0.7), ui["outro2"], font=_fit(d, ui["outro2"], L["OUTRO_SIZE"], W - 120, 36),
           fill="#FFFFFF", anchor="mm")
    if t < 0.3:
        return
    bf = bold(round(L["OUTRO_SIZE"] * 0.55))
    by = cy + L["OUTRO_SIZE"] * 2.6
    pressed_like, pressed_sub = t >= 0.65, t >= 0.85
    like_txt = ("♥ " if pressed_like else "♡ ") + ui["like"]
    sub_txt = ui["subbed"] if pressed_sub else ui["sub"]
    w1, w2 = _tw(d, like_txt, bf) + 90, _tw(d, sub_txt, bf) + 90
    h, gap = bf.size * 2.0, 40
    x = (W - w1 - w2 - gap) / 2
    d.rounded_rectangle([x, by - h / 2, x + w1, by + h / 2], h / 2,
                        fill="#FFFFFF" if pressed_like else None, outline="#FFFFFF", width=3)
    d.text((x + w1 / 2, by), like_txt, font=bf, fill=BG if pressed_like else "#FFFFFF", anchor="mm")
    x += w1 + gap
    d.rounded_rectangle([x, by - h / 2, x + w2, by + h / 2], h / 2, fill="#595959" if pressed_sub else ACCENT)
    d.text((x + w2 / 2, by), sub_txt, font=bf, fill="#FFFFFF", anchor="mm")


def render(spec: dict, layout: str = "landscape") -> Image.Image:
    L = LAYOUTS[layout]
    img = Image.new("RGB", (L["W"], L["H"]), BG)
    d = ImageDraw.Draw(img)
    kind = spec["kind"]
    if kind in ("learn", "review"):
        _draw_learn(img, d, L, spec)
    elif kind == "intro":
        _draw_intro(d, L, spec)
    elif kind == "title":
        _draw_title(d, L, spec)
    elif kind == "outro":
        _draw_outro(d, L, spec)
    else:
        raise ValueError(kind)
    return img
