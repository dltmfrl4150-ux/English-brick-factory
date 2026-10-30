"""M2 화면 시안: 주요 장면 PNG + 한눈에 보는 시트.

사용법:
    python brick_preview.py <출력폴더> <영어학습 CSV> <한국어학습 CSV>
"""
import sys
from pathlib import Path
from PIL import Image, ImageDraw
import brick_csv, brick_frames as bf

def scenes(t, sid):
    s = next(x for x in t.sentences if x.sid == sid)
    blocks = tuple((b.text, b.meaning, b.color) for b in s.blocks)
    base = dict(lang=t.learn_lang, topic=t.topic, group=s.group, speaker=s.speaker,
                translation=s.translation, blocks=blocks)
    n = len(blocks)
    return [
        ("01_소개", dict(kind="intro", lang=t.learn_lang, topic=t.topic, group=s.group, target=t.target, situation=t.situation)),
        ("02_전체뜻_빈블록", dict(base, kind="learn", guide=True)),
        ("03_1번뜻_카운트3", dict(base, kind="learn", meanings=1, countdown=3)),
        ("04_1번공개", dict(base, kind="learn", meanings=1, revealed=1)),
        ("05_2번뜻_카운트5", dict(base, kind="learn", meanings=2, revealed=1, countdown=5)),
        ("06_전체공개", dict(base, kind="learn", meanings=n, revealed=n)),
        ("07_복습_빈블록", dict(base, kind="review", guide=True)),
        ("08_복습_공개", dict(base, kind="review", revealed=n)),
        ("09_아웃트로", dict(kind="outro", lang=t.learn_lang, t=1.0)),
    ]

def sheet(paths, out, cols):
    ims = [Image.open(p) for p in paths]
    w, h = ims[0].size
    s = 0.25 if w > h else 0.2
    tw, th = int(w * s), int(h * s)
    rows = (len(ims) + cols - 1) // cols
    canvas = Image.new("RGB", (cols * (tw + 12) + 12, rows * (th + 12) + 12), "#151515")
    for i, im in enumerate(ims):
        canvas.paste(im.resize((tw, th)), (12 + (i % cols) * (tw + 12), 12 + (i // cols) * (th + 12)))
    canvas.save(out)

out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
jobs = [("en", sys.argv[2], 20), ("ko", sys.argv[3], 12)]   # 시안용 문장 번호
for lang, csvp, sid in jobs:
    t = brick_csv.load_brick_csv(csvp)
    for layout, tag in (("landscape", "16x9"), ("portrait", "9x16")):
        paths = []
        for name, spec in scenes(t, sid):
            p = out / f"{lang}_{tag}_{name}.png"
            bf.render(spec, layout).save(p); paths.append(p)
        sheet(paths, out / f"_시트_{lang}_{tag}.png", 3 if layout == "landscape" else 5)
    # 줄바꿈 확인용: 가장 긴 문장
    longest = max(t.sentences, key=lambda s: len(s.full))
    for layout, tag in (("landscape", "16x9"), ("portrait", "9x16")):
        sp = dict(kind="learn", lang=lang, topic=t.topic, group=longest.group, speaker=longest.speaker,
                  translation=longest.translation, blocks=tuple((b.text, b.meaning, b.color) for b in longest.blocks),
                  meanings=len(longest.blocks), revealed=len(longest.blocks))
        bf.render(sp, layout).save(out / f"{lang}_{tag}_10_긴문장.png")
print("ok")
