"""
브릭 학습 모드 — 롱폼 섬네일 (1920x1080 PNG)

  위: [학습종류] 노란 캡슐 + 흰색 '스피킹연습'
  가운데: 주제 (아주 굵게, 검은 테두리 + 그림자)
  그 아래: 흰 테두리 캡슐 안 부제 (CSV subtitle, 없으면 target)
  아래: 색 블록 범례 6개 (코어문장/대상/장소/시간/목적/문장)

사용법:
    python brick_thumbnail.py <CSV경로> [-o 출력.png]
"""
import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

import brick_csv
import brick_frames

ROOT = Path(__file__).resolve().parent
W, H = 1920, 1080
BG = brick_frames.BG
PILL = "#FFF200"

# 아주 굵은 글꼴: Noto Sans KR 가변 글꼴(Windows 기본 포함)의 굵기 축, 없으면 맑은 고딕 Bold
_NOTO = Path("C:/Windows/Fonts/NotoSansKR-VF.ttf")


def heavy(size, weight=900):
    if _NOTO.exists():
        f = ImageFont.truetype(str(_NOTO), size)
        try:
            f.set_variation_by_axes([weight])
            return f
        except (OSError, ValueError):
            pass
    return brick_frames.bold(size)


# 학습 언어별 문구. 범례 색은 영상 블록 색(연한색 = 섬네일에서 더 선명하게 보임)
TEXT = {
    "en": dict(learn_type="육아영어", practice="스피킹연습",
               legend=[("코어문장", "RED"), ("대상", "ORANGE"), ("장소", "YELLOW"),
                       ("시간", "GREEN"), ("목적", "BLUE"), ("문장", "PURPLE")]),
    "ko": dict(learn_type="Learn Korean", practice="Speaking Practice",
               legend=[("Core", "RED"), ("Object", "ORANGE"), ("Place", "YELLOW"),
                       ("Time", "GREEN"), ("Reason", "BLUE"), ("Subject", "PURPLE")]),
}


def _fit(d, text, size, max_w, weight=900, min_size=40):
    while size > min_size and d.textlength(text, font=heavy(size, weight)) > max_w:
        size -= 4
    return heavy(size, weight)


def render(topic: brick_csv.Topic) -> Image.Image:
    t = TEXT[topic.learn_lang]
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # 위: 학습종류 캡슐 + 스피킹연습
    learn_type = topic.learn_type or t["learn_type"]
    size, pad_x, pill_h, gap = 96, 48, 140, 30
    while True:   # 화면 양옆에 여백이 남도록 글자 크기 조정
        f = heavy(size, 900)
        w1 = d.textlength(learn_type, font=f) + pad_x * 2
        w2 = d.textlength(t["practice"], font=f)
        if w1 + gap + w2 <= W - 400 or size <= 56:
            break
        size -= 4
    x, cy = (W - w1 - gap - w2) / 2, 165
    d.rounded_rectangle([x, cy - pill_h / 2, x + w1, cy + pill_h / 2], pill_h / 2, fill=PILL)
    d.text((x + w1 / 2, cy), learn_type, font=f, fill="#000000", anchor="mm")
    d.text((x + w1 + gap, cy), t["practice"], font=f, fill="#FFFFFF", anchor="lm")

    # 가운데: 주제 (그림자 -> 검은 테두리 + 흰 글자)
    tf = _fit(d, topic.topic, 300, W - 200)
    ty = 470
    shadow = Image.new("L", (W, H), 0)
    ImageDraw.Draw(shadow).text((W / 2 + 10, ty + 12), topic.topic, font=tf, fill=200, anchor="mm",
                                stroke_width=8)
    img.paste(Image.new("RGB", (W, H), "#000000"), (0, 0), shadow.filter(ImageFilter.GaussianBlur(10)))
    d = ImageDraw.Draw(img)
    d.text((W / 2, ty), topic.topic, font=tf, fill="#FFFFFF", anchor="mm",
           stroke_width=8, stroke_fill="#000000")

    # 부제 캡슐 (흰 테두리)
    sub = topic.subtitle or topic.target
    if sub:
        sf = _fit(d, sub, 84, W - 500, 800)
        sw, sh, sy = d.textlength(sub, font=sf) + 140, 120, 735
        d.rounded_rectangle([(W - sw) / 2, sy - sh / 2, (W + sw) / 2, sy + sh / 2], sh / 2,
                            outline="#FFFFFF", width=4)
        d.text((W / 2, sy), sub, font=sf, fill="#FFFFFF", anchor="mm")

    # 아래: 색 블록 범례
    lf = heavy(64, 800)
    bh, bpad, bgap, by = 120, 44, 8, 940
    widths = [d.textlength(label, font=lf) + bpad * 2 for label, _ in t["legend"]]
    total = sum(widths) + bgap * (len(widths) - 1)
    if total > W - 160:   # 넘치면 글자를 줄임
        lf = heavy(round(64 * (W - 160) / total), 800)
        widths = [d.textlength(label, font=lf) + bpad * 2 for label, _ in t["legend"]]
        total = sum(widths) + bgap * (len(widths) - 1)
    x = (W - total) / 2
    for (label, cat), w in zip(t["legend"], widths):
        d.rounded_rectangle([x, by - bh / 2, x + w, by + bh / 2], 22, fill=brick_csv.PALETTE[cat]["light"])
        d.text((x + w / 2, by), label, font=lf, fill="#FFFFFF", anchor="mm")
        x += w + bgap
    return img


def make_thumbnail(csv_path, out_png=None) -> Path:
    """롱폼 메타데이터가 가리키는 {topic}_섬네일.png로 저장."""
    topic = brick_csv.load_brick_csv(csv_path)
    out_png = Path(out_png or ROOT / "output" / f"{topic.topic}_섬네일.png")
    out_png.parent.mkdir(parents=True, exist_ok=True)
    render(topic).save(out_png)
    return out_png


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="브릭 학습 모드 롱폼 섬네일")
    ap.add_argument("csv")
    ap.add_argument("-o", "--output")
    a = ap.parse_args()
    print(make_thumbnail(a.csv, a.output))
