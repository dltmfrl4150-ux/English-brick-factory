"""
롱폼 섬네일 (1920x1080 PNG) 생성

- 완전 블랙 배경, 그림 요소 없음
- 좌측 상단 카테고리: 육아영어(노랑) / 스피킹연습(흰색)
- 정중앙 메인 타이틀: CSV '주제' (없으면 파일명)
- 타이틀 아래 캡슐형 대상 설명
- 하단 6개 구문 블록: 테두리만 순색 3px (빨/주/노/초/파/보)

사용법:
    python thumbnail.py [CSV경로] [-o 출력.png]
"""
import argparse
from pathlib import Path

from PIL import Image, ImageDraw

from shadowing_render import (DEFAULT_CSV, DEFAULT_OUT, FONT_KO, PALETTE, draw_border, font,
                              load_topic)

W, H = 1920, 1080
BORDER = 3

CATEGORIES = [("육아영어", "#FFFF00"), ("스피킹연습", "#FFFFFF")]
TARGET_TEXT = "가족 구성원 별 : 영아 / 유아 / 초등이상"
BLOCKS = [("코어문장", "빨강"), ("대상", "주황"), ("장소", "노랑"),
          ("시간", "초록"), ("목적", "파랑"), ("문장", "보라")]

TITLE_Y = 440
CAPSULE_Y = 660
BLOCK_Y = 900


def fit(draw, text, path, size, max_w, min_size=60):
    while size > min_size and draw.textlength(text, font=font(path, size)) > max_w:
        size -= 4
    return font(path, size)


def make_thumbnail(csv_path, out_png=None, title=None) -> Path:
    csv_path = Path(csv_path)
    title = title or load_topic(csv_path)
    out_png = Path(out_png or DEFAULT_OUT / f"{title}_섬네일.png")
    out_png.parent.mkdir(parents=True, exist_ok=True)
    render_thumbnail(title).save(out_png)
    return out_png


def render_thumbnail(title) -> Image.Image:
    """섬네일 이미지 (롱폼 인트로 화면으로도 사용)."""
    img = Image.new("RGB", (W, H), "#000000")
    d = ImageDraw.Draw(img)

    # 좌측 상단 카테고리
    x = 90
    cat_font = font(FONT_KO, 64)
    for text, color in CATEGORIES:
        d.text((x, 110), text, font=cat_font, fill=color, anchor="lm")
        x += d.textlength(text, font=cat_font) + 36

    # 메인 타이틀 (가장 크고 굵게)
    d.text((W / 2, TITLE_Y), title, font=fit(d, title, FONT_KO, 260, W - 240),
           fill="#FFFFFF", anchor="mm")

    # 캡슐형 대상 설명 (얇은 테두리)
    cap_font = font(FONT_KO, 52)
    tw = d.textlength(TARGET_TEXT, font=cap_font)
    cw, ch = tw + 100, 96
    d.rounded_rectangle([(W - cw) / 2, CAPSULE_Y - ch / 2, (W + cw) / 2, CAPSULE_Y + ch / 2],
                        radius=ch / 2, outline="#FFFFFF", width=2)
    d.text((W / 2, CAPSULE_Y), TARGET_TEXT, font=cap_font, fill="#FFFFFF", anchor="mm")

    # 하단 구문 블록 (맞붙인 테두리 브릭)
    blk_font = font(FONT_KO, 54)
    widths = [d.textlength(t, font=blk_font) + 88 for t, _ in BLOCKS]
    bh = 124
    x = (W - (sum(widths) - BORDER * (len(BLOCKS) - 1))) / 2
    for (text, color_name), w in zip(BLOCKS, widths):
        draw_border(d, [x, BLOCK_Y - bh / 2, x + w, BLOCK_Y + bh / 2], PALETTE[color_name], BORDER)
        d.text((x + w / 2, BLOCK_Y), text, font=blk_font, fill="#FFFFFF", anchor="mm")
        x += w - BORDER
    return img


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("csv", nargs="?", default=str(DEFAULT_CSV))
    ap.add_argument("-o", "--output")
    args = ap.parse_args()
    print(make_thumbnail(args.csv, args.output))
