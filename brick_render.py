"""
브릭 학습 모드 — 음성 + 타임라인 + 영상 렌더링 (M3: 롱폼)

CSV(brick_csv) -> Edge TTS -> 실제 음성 길이 기준 타임라인 -> brick_frames 정지 프레임 -> ffmpeg MP4
기존 엔진(shadowing_render)의 TTS 캐시, 무음 처리, Timeline, encode를 그대로 재사용한다 (기존 파일 수정 없음).

흐름 (그룹 하나 = 롱폼 하나, docs/01_개발명세.md 4장)
  STEP 1 주제 소개 -> STEP 2 문장별 블록 학습 -> STEP 3 이어서 말해보기 -> STEP 4 아웃트로

사용법:
    python brick_render.py <CSV경로> [--group 영아] [-o 출력폴더]
"""
import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path

import brick_csv
import brick_frames
import shadowing_render as sr

ROOT = Path(__file__).resolve().parent
FRAME_DIR = sr.BUILD / "frames_brick"

# 설정값 (ms / 배수). config.json의 "brick" 항목이 있으면 덮어씀 (M6에서 런처 연결)
DEFAULTS = dict(
    lead_pad_ms=300,
    intro_min_ms=3000,
    full_ko_wait_factor=1.0,
    first_block_countdown_s=3,
    next_block_countdown_s=5,
    en_wait_factor=1.5,
    en_repeat_long=2,
    en_repeat_shorts=1,
    review_wait_factor=1.5,
    review_after_ms=1000,
    outro_ms=4000,
    shorts_target_s=175,
)

SPEAKER_ROLE = {"엄마": "mom", "아이": "kid"}
OUTRO_STEPS = (0.0, 0.3, 0.65, 0.85)   # brick_frames 아웃트로의 화면이 바뀌는 지점
POP_FRAMES = 8                          # 8프레임 ≈ 0.27초 (lead_pad 0.3초 안에서 끝남)
POP_START_SCALE = 0.5

# 프레임 디자인(brick_frames.py)이 바뀌면 캐시도 새로 만들도록 소스 해시를 키에 포함
_FRAMES_VER = hashlib.md5(Path(brick_frames.__file__).read_bytes()).hexdigest()[:8]


def load_settings(config_path=ROOT / "config.json"):
    """(voices, settings). 토큰 등 다른 항목은 읽지 않는다."""
    cfg = {}
    if Path(config_path).exists():
        cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
    return cfg.get("voices", {}), {**DEFAULTS, **cfg.get("brick", {})}


# ───────────────────────── 음성 ─────────────────────────
class Voices:
    """학습 언어 기준으로 블록/문장 음성과 뜻/번역 음성을 화자별로 고른다 (명세 6장)."""

    def __init__(self, learn_lang, voices, log=print):
        self.learn = learn_lang
        self.meaning = "ko" if learn_lang == "en" else "en"
        self.voices = {k: voices.get(k) or v for k, v in sr.DEFAULT_VOICES.items()}
        self.log = log
        self._warned = set()

    def _role(self, speaker):
        role = SPEAKER_ROLE.get(speaker)
        if role is None:
            if speaker not in self._warned:
                self.log(f"  [경고] 알 수 없는 화자 '{speaker}' -> 엄마 음성 사용")
                self._warned.add(speaker)
            role = "mom"
        return role

    def learn_voice(self, speaker):
        return self.voices[f"{self.learn}_{self._role(speaker)}"]

    def meaning_voice(self, speaker):
        return self.voices[f"{self.meaning}_{self._role(speaker)}"]


class Clips:
    """(텍스트, 음성) -> 앞뒤 무음을 제거한 AudioSegment. 생성은 한 번에 모아서 병렬로."""

    def __init__(self):
        self.jobs = set()
        self.segs = {}

    def need(self, text, voice):
        self.jobs.add((text, voice))
        return (text, voice)

    def build(self, log=print):
        sr.TTS_DIR.mkdir(parents=True, exist_ok=True)

        async def run():
            sem = asyncio.Semaphore(4)
            await asyncio.gather(*(sr.synth(t, v, sem, log) for t, v in self.jobs))

        log(f"[TTS] {len(self.jobs)}개 클립 생성/캐시 확인")
        asyncio.run(run())
        for t, v in self.jobs:
            seg = sr.AudioSegment.from_file(sr.tts_path(t, v))
            self.segs[(t, v)] = sr.trim(seg).set_frame_rate(sr.SAMPLE_RATE).set_channels(1)

    def __getitem__(self, key):
        return self.segs[key]


# ───────────────────────── 타임라인 ─────────────────────────
def render_frame(spec, layout):
    key = hashlib.md5(repr((_FRAMES_VER, layout, sorted(spec.items()))).encode()).hexdigest()[:16]
    out = FRAME_DIR / f"{key}.png"
    if not out.exists():
        brick_frames.render(spec, layout).save(out)
    return out


class BrickTimeline(sr.Timeline):
    """기존 Timeline에서 show()/play()만 재정의: brick_frames로 그리고, 음성 뒤 유지 여백 없음."""

    def __init__(self, layout, lead_pad_ms):
        super().__init__()
        self.layout = layout
        self.lead_pad_ms = lead_pad_ms
        self.events = []   # (시작 ms, 설명) — 타이밍 검증용 로그

    def log_event(self, text):
        self.events.append((len(self.audio), text))

    def show(self, spec, pop=False):
        frame = render_frame(spec, self.layout)
        if self.shots and self.shots[-1][0] == frame:
            return
        t0 = len(self.audio)
        if pop:
            for k in range(POP_FRAMES):
                t = (k + 1) / POP_FRAMES
                scale = POP_START_SCALE + (1 - POP_START_SCALE) * sr.ease_out_back(t)
                self.shots.append((render_frame({**spec, "pop": round(scale, 3)}, self.layout),
                                   t0 + k * 1000 / sr.FPS))
            t0 += POP_FRAMES * 1000 / sr.FPS
        self.shots.append((frame, t0))

    def play(self, seg, label=""):
        """[lead_pad 무음] + 음성. 음성 뒤에는 여백을 두지 않는다."""
        self.silence(self.lead_pad_ms)
        self.log_event(f"음성 {len(seg)}ms {label}")
        self.audio += seg

    def wait(self, ms, label=""):
        self.log_event(f"대기 {int(ms)}ms {label}")
        self.silence(ms)

    def countdown(self, spec, seconds):
        for n in range(seconds, 0, -1):
            self.show({**spec, "countdown": n})
            self.wait(1000, f"카운트다운 {n}")


def build_timeline(topic, group, sentences, voices, cfg, layout="landscape", shorts=False, log=print):
    lang = topic.learn_lang
    clips = Clips()
    # 필요한 음성 목록 (주제는 뜻 언어 엄마 음성)
    k_topic = clips.need(topic.topic, voices.meaning_voice("엄마"))
    keys = {}
    for s in sentences:
        lv, mv = voices.learn_voice(s.speaker), voices.meaning_voice(s.speaker)
        n = len(s.blocks)
        keys[s.sid] = dict(
            translation=clips.need(s.translation, mv),
            full=clips.need(s.full, lv),
            meaning=[clips.need(s.meaning_prefix(k), mv) for k in range(1, n + 1)],
            prefix=[clips.need(s.prefix(k), lv) for k in range(1, n + 1)],
        )
    clips.build(log)

    FRAME_DIR.mkdir(parents=True, exist_ok=True)
    tl = BrickTimeline(layout, cfg["lead_pad_ms"])
    repeat = cfg["en_repeat_shorts"] if shorts else cfg["en_repeat_long"]

    # STEP 1 · 주제 소개 (최소 intro_min_ms 유지)
    tl.show(dict(kind="intro", lang=lang, topic=topic.topic, group=group,
                 target=topic.target, situation=topic.situation))
    tl.mark("STEP 1 주제 소개")
    start = len(tl.audio)
    tl.play(clips[k_topic], f"주제: {topic.topic}")
    rest = cfg["intro_min_ms"] - (len(tl.audio) - start)
    if rest > 0:
        tl.wait(rest, "소개 최소 유지")

    def base(s):
        return dict(lang=lang, topic=topic.topic, group=group, speaker=s.speaker, translation=s.translation,
                    blocks=tuple((b.text, b.meaning, b.color) for b in s.blocks))

    # STEP 2 · 문장별 블록 학습
    for i, s in enumerate(sentences):
        k = keys[s.sid]
        full = clips[k["full"]]
        learn = dict(base(s), kind="learn")
        log_s = f"문장 {s.sid}"
        tl.show(dict(learn, guide=True))
        tl.mark(f"STEP 2 {s.full}" if i else f"STEP 2 블록 학습 · {s.full}")
        tl.play(clips[k["translation"]], f"{log_s} 전체 번역")
        tl.wait(len(full) * cfg["full_ko_wait_factor"], f"{log_s} 전체 정답 길이 x{cfg['full_ko_wait_factor']}")
        for j in range(1, len(s.blocks) + 1):
            # j번 뜻 추가 -> 누적 뜻 음성 -> 카운트다운
            shown = dict(learn, meanings=j, revealed=j - 1)
            tl.show(shown)
            tl.play(clips[k["meaning"][j - 1]], f"{log_s} 누적 뜻 {j}")
            tl.countdown(shown, cfg["first_block_countdown_s"] if j == 1 else cfg["next_block_countdown_s"])
            # j번 블록 공개(팝업) -> 누적 문장 음성 반복
            seg = clips[k["prefix"][j - 1]]
            tl.show(dict(learn, meanings=j, revealed=j), pop=True)
            for r in range(repeat):
                if r:
                    tl.play(seg, f"{log_s} 누적 문장 {j} (반복)")
                else:
                    tl.play(seg, f"{log_s} 누적 문장 {j}")
                tl.wait(len(seg) * cfg["en_wait_factor"], f"{log_s} 따라 말하기 x{cfg['en_wait_factor']}")

    # STEP 3 · 이어서 말해보기 (쇼츠는 없음)
    if not shorts:
        for i, s in enumerate(sentences):
            k = keys[s.sid]
            full = clips[k["full"]]
            review = dict(base(s), kind="review")
            tl.show(dict(review, guide=True))
            if i == 0:
                tl.mark("STEP 3 이어서 말해보기")
            tl.play(clips[k["translation"]], f"복습 {s.sid} 전체 번역")
            tl.wait(len(full) * cfg["review_wait_factor"], f"복습 {s.sid} 말해보기 x{cfg['review_wait_factor']}")
            tl.show(dict(review, revealed=len(s.blocks)))
            tl.play(full, f"복습 {s.sid} 전체 정답")
            tl.wait(cfg["review_after_ms"], f"복습 {s.sid} 다음 문장 전")

    # STEP 4 · 아웃트로 (버튼 애니메이션)
    for n, t in enumerate(OUTRO_STEPS):
        tl.show(dict(kind="outro", lang=lang, t=t))
        if n == 0:
            tl.mark("STEP 4 아웃트로")
        nxt = OUTRO_STEPS[n + 1] if n + 1 < len(OUTRO_STEPS) else 1.0
        tl.wait(cfg["outro_ms"] * (nxt - t), f"아웃트로 t={t}")
    return tl


# ───────────────────────── 고수준 API ─────────────────────────
def output_name(topic, group, layout="landscape"):
    return f"{topic.topic}_{group}_롱폼_16x9.mp4" if layout == "landscape" else f"{topic.topic}_{group}_9x16.mp4"


def render_long(csv_path, group=None, out_dir=ROOT / "output", log=print, progress=lambda f: None):
    """그룹 하나의 16:9 롱폼 렌더링. 반환: (mp4 경로, 길이 초, 타임라인)."""
    topic = brick_csv.load_brick_csv(csv_path)
    groups = topic.groups()
    group = group or next(iter(groups))
    if group not in groups:
        raise ValueError(f"그룹 '{group}'이 CSV에 없습니다 (있는 그룹: {', '.join(groups)})")
    review = [f"문장 {s.sid}: {b.text}" for s in groups[group] for b in s.blocks if b.needs_review]
    if review:
        log(f"[경고] 확인 필요(needs_review) 블록 {len(review)}개: " + " / ".join(review))

    voice_cfg, cfg = load_settings()
    voices = Voices(topic.learn_lang, voice_cfg, log)
    log(f"[브릭] {topic.topic} / {group} / {len(groups[group])}문장 / 학습 언어 {topic.learn_lang}")
    tl = build_timeline(topic, group, groups[group], voices, cfg, "landscape", shorts=False, log=log)
    progress(0.15)
    out = Path(out_dir) / output_name(topic, group)
    sec = sr.encode(tl, out, log, lambda f: progress(0.15 + 0.85 * f))
    progress(1.0)

    # 타이밍 검증용 로그 (build/ — 저장소에 올리지 않음)
    debug = sr.BUILD / f"{out.stem}_timeline.json"
    debug.write_text(json.dumps(dict(
        duration_s=sec,
        markers=[(label, round(ms)) for label, ms in tl.markers],
        events=[(ms, text) for ms, text in tl.events],
    ), ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"[완료] {out} ({sec:.1f}초), 타이밍 로그: {debug}")
    return out, sec, tl


def main():
    ap = argparse.ArgumentParser(description="브릭 학습 모드 롱폼 렌더링")
    ap.add_argument("csv")
    ap.add_argument("--group", help="렌더링할 그룹 (기본: CSV의 첫 그룹)")
    ap.add_argument("-o", "--out", default=str(ROOT / "output"))
    a = ap.parse_args()
    try:
        render_long(a.csv, a.group, a.out)
    except brick_csv.BrickCSVError as e:
        print(e)
        sys.exit(1)


if __name__ == "__main__":
    main()
