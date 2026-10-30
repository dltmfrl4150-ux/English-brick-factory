"""
브릭 학습 모드 — 음성 + 타임라인 + 영상 렌더링 (M3: 롱폼)

CSV(brick_csv) -> Edge TTS -> 실제 음성 길이 기준 타임라인 -> brick_frames 정지 프레임 -> ffmpeg MP4
기존 엔진(shadowing_render)의 TTS 캐시, 무음 처리, Timeline, encode를 그대로 재사용한다 (기존 파일 수정 없음).

흐름 (그룹 하나 = 롱폼 하나, docs/01_개발명세.md 4장)
  주제 소개 -> STEP 1 문장별 블록학습 -> [STEP 2 제목 카드 1초] -> STEP 2 이어서 말해보기 -> 아웃트로
  (명세 4장의 STEP 1~4 중 화면에 번호를 붙이는 것은 블록학습과 이어서 말해보기 두 단계)

사용법:
    python brick_render.py <CSV경로> [--group 영아] [--format long|shorts|all] [-o 출력폴더]
    --group 없으면 모든 그룹
    -> output/{topic}_{group}_롱폼_16x9.mp4 + 같은 이름 .json (제목·설명·태그·문장별 챕터)
    -> output/{topic}_{group}_숏폼_{n}_9x16.mp4 + .json (그룹 안에서 shorts_target_s 이하로 배분)
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
    shorts_intro_min_ms=1500,   # 쇼츠의 주제 소개는 짧게 (주제 음성이 더 길면 음성 길이)
    full_ko_wait_factor=1.0,
    first_block_countdown_s=3,
    next_block_countdown_s=5,
    en_wait_factor=1.5,
    block_repeat_long=1,     # 누적 문장(완성 전) 재생 횟수
    final_repeat_long=2,     # 완성 문장 재생 횟수
    block_repeat_shorts=1,
    final_repeat_shorts=1,
    review_wait_factor=1.5,
    review_after_ms=1000,
    step_title_ms=1000,      # STEP 1(블록학습)과 STEP 2(이어서 말해보기) 사이 제목 카드
    outro_ms=4000,
    shorts_target_s=175,
)

SPEAKER_ROLE = {"엄마": "mom", "아이": "kid"}
OUTRO_STEPS = (0.0, 0.3, 0.65, 0.85)   # brick_frames 아웃트로의 화면이 바뀌는 지점
POP_FRAMES = 8                          # 8프레임 ≈ 0.27초 (lead_pad 0.3초 안에서 끝남)
POP_START_SCALE = 0.5

# 프레임 디자인(brick_frames.py, 마이크 아이콘)이 바뀌면 캐시도 새로 만들도록 해시를 키에 포함
_FRAMES_VER = hashlib.md5(Path(brick_frames.__file__).read_bytes()
                          + brick_frames.MIC_IMAGE.read_bytes()).hexdigest()[:8]


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


_SEGS = {}   # (텍스트, 음성) -> 무음 제거한 음성. 쇼츠 길이 계산처럼 여러 번 만들 때 다시 읽지 않도록


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

        todo = self.jobs - _SEGS.keys()
        if todo:
            log(f"[TTS] {len(todo)}개 클립 생성/캐시 확인")
            asyncio.run(run())
        for t, v in todo:
            seg = sr.AudioSegment.from_file(sr.tts_path(t, v))
            _SEGS[(t, v)] = sr.trim(seg).set_frame_rate(sr.SAMPLE_RATE).set_channels(1)
        self.segs = {k: _SEGS[k] for k in self.jobs}

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

    def __init__(self, layout, lead_pad_ms, dry=False):
        super().__init__()
        self.layout = layout
        self.lead_pad_ms = lead_pad_ms
        self.dry = dry     # True면 프레임을 그리지 않고 길이만 계산 (쇼츠 배분용)
        self.events = []   # (시작 ms, 설명) — 타이밍 검증용 로그

    def log_event(self, text):
        self.events.append((len(self.audio), text))

    def frame(self, spec):
        return repr(sorted(spec.items())) if self.dry else render_frame(spec, self.layout)

    def show(self, spec, pop=False):
        frame = self.frame(spec)
        if self.shots and self.shots[-1][0] == frame:
            return
        t0 = len(self.audio)
        if pop:
            for k in range(POP_FRAMES):
                t = (k + 1) / POP_FRAMES
                scale = POP_START_SCALE + (1 - POP_START_SCALE) * sr.ease_out_back(t)
                self.shots.append((self.frame({**spec, "pop": round(scale, 3)}),
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


def build_timeline(topic, group, sentences, voices, cfg, layout="landscape", shorts=False, log=print, dry=False):
    """dry=True: 프레임 없이 길이만 계산."""
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
            # 마지막 블록의 누적 뜻은 블록 뜻을 잇지 않고 원래 번역 음성 (translation)을 쓴다
            meaning=[clips.need(s.meaning_prefix(k), mv) for k in range(1, n)],
            prefix=[clips.need(s.prefix(k), lv) for k in range(1, n + 1)],
        )
    clips.build(log)

    FRAME_DIR.mkdir(parents=True, exist_ok=True)
    tl = BrickTimeline(layout, cfg["lead_pad_ms"], dry)
    mode = "shorts" if shorts else "long"
    block_repeat, final_repeat = cfg[f"block_repeat_{mode}"], cfg[f"final_repeat_{mode}"]

    # STEP 1 · 주제 소개 (최소 intro_min_ms 유지, 쇼츠는 짧게)
    tl.show(dict(kind="intro", lang=lang, topic=topic.topic, group=group,
                 target=topic.target, situation=topic.situation))
    tl.mark("주제 소개")
    start = len(tl.audio)
    tl.play(clips[k_topic], f"주제: {topic.topic}")
    rest = cfg["shorts_intro_min_ms" if shorts else "intro_min_ms"] - (len(tl.audio) - start)
    if rest > 0:
        tl.wait(rest, "소개 최소 유지")

    def base(s):
        return dict(lang=lang, learn_type=topic.learn_type, topic=topic.topic, group=group, speaker=s.speaker,
                    translation=s.translation, blocks=tuple((b.text, b.meaning, b.color) for b in s.blocks))

    # STEP 1 · 문장별 블록 학습 (명세 4장의 STEP 2. 화면에는 STEP 1로 표시)
    for i, s in enumerate(sentences):
        k = keys[s.sid]
        full = clips[k["full"]]
        learn = dict(base(s), kind="learn")
        log_s = f"문장 {s.sid}"
        tl.show(dict(learn, guide=True))
        tl.mark(f"{brick_frames.step_text(lang, 'learn')} · {s.full}" if i == 0 else s.full)
        tl.play(clips[k["translation"]], f"{log_s} 전체 번역")
        tl.wait(len(full) * cfg["full_ko_wait_factor"], f"{log_s} 전체 정답 길이 x{cfg['full_ko_wait_factor']}")
        n = len(s.blocks)
        for j in range(1, n + 1):
            last = j == n
            # j번 뜻 추가 -> 누적 뜻 음성 (마지막은 원래 번역 음성) -> 카운트다운
            shown = dict(learn, meanings=j, revealed=j - 1)
            tl.show(shown)
            if last:
                tl.play(clips[k["translation"]], f"{log_s} 누적 뜻 {j} (원래 번역)")
            else:
                tl.play(clips[k["meaning"][j - 1]], f"{log_s} 누적 뜻 {j}")
            tl.countdown(shown, cfg["first_block_countdown_s"] if j == 1 else cfg["next_block_countdown_s"])
            # j번 블록 공개(팝업) -> 누적 문장 음성 (완성 문장만 반복)
            seg = clips[k["prefix"][j - 1]]
            tl.show(dict(learn, meanings=j, revealed=j), pop=True)
            for r in range(final_repeat if last else block_repeat):
                if r:
                    tl.play(seg, f"{log_s} 누적 문장 {j} (반복)")
                else:
                    tl.play(seg, f"{log_s} 누적 문장 {j}")
                tl.wait(len(seg) * cfg["en_wait_factor"], f"{log_s} 따라 말하기 x{cfg['en_wait_factor']}")

    # STEP 2 · 이어서 말해보기 (명세 4장의 STEP 3. 쇼츠는 없음). 앞에 제목 카드
    if not shorts:
        tl.show(dict(kind="title", lang=lang, step="review"))
        tl.mark(brick_frames.step_text(lang, "review"))
        tl.wait(cfg["step_title_ms"], "STEP 2 제목 카드")
        for s in sentences:
            k = keys[s.sid]
            full = clips[k["full"]]
            review = dict(base(s), kind="review")
            tl.show(dict(review, guide=True))
            tl.play(clips[k["translation"]], f"복습 {s.sid} 전체 번역")
            tl.wait(len(full) * cfg["review_wait_factor"], f"복습 {s.sid} 말해보기 x{cfg['review_wait_factor']}")
            tl.show(dict(review, revealed=len(s.blocks)))
            tl.play(full, f"복습 {s.sid} 전체 정답")
            tl.wait(cfg["review_after_ms"], f"복습 {s.sid} 다음 문장 전")

    # STEP 4 · 아웃트로 (버튼 애니메이션)
    for n, t in enumerate(OUTRO_STEPS):
        tl.show(dict(kind="outro", lang=lang, t=t))
        if n == 0:
            tl.mark("아웃트로")
        nxt = OUTRO_STEPS[n + 1] if n + 1 < len(OUTRO_STEPS) else 1.0
        tl.wait(cfg["outro_ms"] * (nxt - t), f"아웃트로 t={t}")
    return tl


# ───────────────────────── 유튜브 메타데이터 ─────────────────────────
# 영상과 같은 이름의 .json (uploaders.load_meta가 읽음). 렌더링 후 json을 열어 문구를 검수·수정하면 된다.
META = {
    "en": dict(
        title="[{topic}] {group} {learn_type} 블록 스피킹 연습",
        intro="색깔 블록으로 문장을 하나씩 조립하며 영어로 말해보는 {learn_type} 스피킹 연습!\n"
              "STEP 1 블록학습으로 문장을 쌓고, STEP 2 이어서 말해보기로 한 번에 말해보세요.",
        chapters="[타임라인 챕터]",
        sentences="[오늘의 문장]",
        tags=["육아영어", "엄마표영어", "영어스피킹", "영어회화", "조립식영어"],
        learn_type="육아영어",
        short_title="[{topic}] {group} {learn_type} 블록 스피킹 {n}/{total} #Shorts",
        short_intro="색깔 블록으로 문장을 하나씩 조립하며 영어로 말해보세요!",
    ),
    "ko": dict(
        title="[{topic}] Learn Korean with Color Blocks · {group}",
        intro="Build Korean sentences block by block and say them out loud!\n"
              "STEP 1: build each sentence. STEP 2: say it all in one go.",
        chapters="[Chapters]",
        sentences="[Today's sentences]",
        tags=["Learn Korean", "Korean speaking", "Korean for parents", "Korean phrases", "Korean practice"],
        learn_type="",
        short_title="[{topic}] Learn Korean · {group} {n}/{total} #Shorts",
        short_intro="Build Korean sentences block by block and say them out loud!",
    ),
}


def chapters_for_youtube(markers, total_ms):
    """유튜브 챕터 조건(00:00 시작, 3개 이상, 각 10초 이상)에 맞춤.
    3초짜리 주제 소개는 첫 문장 챕터에, 4초짜리 아웃트로는 마지막 챕터에 합친다."""
    body = [(label, ms) for label, ms in markers if label not in ("주제 소개", "아웃트로")]
    if body:
        body[0] = (body[0][0], 0)
    starts = [int(ms // 1000) for _, ms in body] + [int(total_ms // 1000)]
    problems = []
    if len(body) < 3:
        problems.append("챕터가 3개 미만")
    if any(b - a < sr.YT_CHAPTER_MIN_SEC for a, b in zip(starts, starts[1:])):
        problems.append("10초 미만 구간 있음")
    return [{"time": sr.fmt_ts(ms / 1000), "title": label} for label, ms in body], problems


def long_meta(topic, group, sentences, tl, sec, thumbnail_path):
    m = META[topic.learn_lang]
    learn_type = topic.learn_type or m["learn_type"]
    gname = brick_frames.name(topic.learn_lang, group)
    chapters, problems = chapters_for_youtube(tl.markers, len(tl.audio))
    tags = list(dict.fromkeys(m["tags"] + [x for x in (learn_type, topic.topic, gname) if x]))
    lines = "\n".join(f"{s.full} — {s.translation}" for s in sentences)
    desc = (f"{m['intro'].format(learn_type=learn_type).strip()}\n\n"
            f"{m['sentences']}\n{lines}\n\n"
            f"{m['chapters']}\n" + "\n".join(f"{c['time']} {c['title']}" for c in chapters) + "\n\n"
            + " ".join("#" + t.replace(" ", "") for t in tags[:5]))
    title = " ".join(m["title"].format(topic=topic.topic, group=gname, learn_type=learn_type).split())
    return {
        "kind": "long",
        "title": title,
        "description": desc,
        "tags": tags,
        "thumbnail": str(thumbnail_path),   # 런처 '섬네일 생성'으로 만든 파일. 없으면 업로드 시 건너뜀
        "chapters": chapters,
        "duration_sec": round(sec, 1),
    }, problems


def short_meta(topic, group, sentences, n, total, sec):
    m = META[topic.learn_lang]
    learn_type = topic.learn_type or m["learn_type"]
    gname = brick_frames.name(topic.learn_lang, group)
    tags = list(dict.fromkeys(["Shorts"] + m["tags"] + [x for x in (learn_type, topic.topic, gname) if x]))
    lines = "\n".join(f"{s.full} — {s.translation}" for s in sentences)
    title = " ".join(m["short_title"].format(topic=topic.topic, group=gname, learn_type=learn_type,
                                              n=n, total=total).split())
    return {
        "kind": "short",
        "title": title,
        "description": f"{m['short_intro']}\n\n{lines}\n\n"
                       + " ".join("#" + t.replace(" ", "") for t in tags[1:6]) + " #Shorts",
        "tags": tags,
        "duration_sec": round(sec, 1),
    }


# ───────────────────────── 쇼츠 배분 ─────────────────────────
def split_shorts(topic, group, sentences, voices, cfg, log=print):
    """실제 음성 길이로 문장별 쇼츠 길이를 미리 계산해, 문장 순서대로 shorts_target_s 이하가 되게 나눈다.
    편 수는 가장 적게, 그 편 수 안에서는 가장 긴 편이 가장 짧아지도록 고르게 나눔 (그룹 하나 안에서만).
    반환: [[Sentence, ...], ...]"""
    def length(ss):
        return len(build_timeline(topic, group, ss, voices, cfg, "portrait", shorts=True,
                                  log=lambda *_: None, dry=True).audio)

    overhead = length([])                       # 주제 소개 + 아웃트로
    each = [length([s]) - overhead for s in sentences]
    cap = cfg["shorts_target_s"] * 1000 - overhead
    for s, ms in zip(sentences, each):
        if ms > cap:
            log(f"  [주의] 문장 {s.sid}만으로도 {(ms + overhead) / 1000:.0f}초 — {cfg['shorts_target_s']}초를 넘습니다.")

    # 최소 편 수: 앞에서부터 채우기
    parts, cur = 1, 0
    for ms in each:
        if cur and cur + ms > cap:
            parts, cur = parts + 1, 0
        cur += ms

    # 그 편 수로 연속 구간 분할 중 가장 긴 편이 최소인 것 (문장 수가 적어 전수 DP)
    n = len(each)
    pre = [0]
    for ms in each:
        pre.append(pre[-1] + ms)
    best = {(0, 0): (0, [])}                    # (사용 문장 수, 편 수) -> (최대 길이, 경계)
    for k in range(1, parts + 1):
        for i in range(k, n + 1):
            cands = [(max(best[(j, k - 1)][0], pre[i] - pre[j]), best[(j, k - 1)][1] + [i])
                     for j in range(k - 1, i) if (j, k - 1) in best]
            if cands:
                best[(i, k)] = min(cands)
    cuts = [0] + best[(n, parts)][1]
    out = [sentences[a:b] for a, b in zip(cuts, cuts[1:])]
    for k, ss in enumerate(out, 1):
        est = (overhead + sum(each[sentences.index(s)] for s in ss)) / 1000
        log(f"  쇼츠 {k}/{len(out)}: 문장 {ss[0].sid}~{ss[-1].sid} ({len(ss)}문장, 예상 {est:.1f}초)")
    return out


# ───────────────────────── 고수준 API ─────────────────────────
def output_name(topic, group, layout="landscape", n=None):
    return f"{topic.topic}_{group}_롱폼_16x9.mp4" if layout == "landscape" else f"{topic.topic}_{group}_숏폼_{n}_9x16.mp4"


def _load_group(csv_path, group, log):
    topic = brick_csv.load_brick_csv(csv_path)
    groups = topic.groups()
    group = group or next(iter(groups))
    if group not in groups:
        raise ValueError(f"그룹 '{group}'이 CSV에 없습니다 (있는 그룹: {', '.join(groups)})")
    review = [f"문장 {s.sid}: {b.text}" for s in groups[group] for b in s.blocks if b.needs_review]
    if review:
        log(f"[경고] 확인 필요(needs_review) 블록 {len(review)}개: " + " / ".join(review))
    return topic, group, groups[group]


def _write_debug(out, sec, tl):
    """타이밍 검증용 로그 (build/ — 저장소에 올리지 않음)."""
    debug = sr.BUILD / f"{out.stem}_timeline.json"
    debug.write_text(json.dumps(dict(
        duration_s=sec,
        markers=[(label, round(ms)) for label, ms in tl.markers],
        events=[(ms, text) for ms, text in tl.events],
    ), ensure_ascii=False, indent=1), encoding="utf-8")
    return debug


def render_shorts(csv_path, group=None, out_dir=ROOT / "output", log=print, progress=lambda f: None):
    """그룹 하나를 9:16 쇼츠 여러 편으로 렌더링 + 편마다 메타데이터 json. 반환: [mp4 경로]."""
    topic, group, sentences = _load_group(csv_path, group, log)
    voice_cfg, cfg = load_settings()
    voices = Voices(topic.learn_lang, voice_cfg, log)
    log(f"[브릭 쇼츠] {topic.topic} / {group} / {len(sentences)}문장 / 학습 언어 {topic.learn_lang}")
    parts = split_shorts(topic, group, sentences, voices, cfg, log)
    outs = []
    for n, ss in enumerate(parts, 1):
        span = 1 / len(parts)
        base = (n - 1) * span
        tl = build_timeline(topic, group, ss, voices, cfg, "portrait", shorts=True, log=log)
        out = Path(out_dir) / output_name(topic, group, "portrait", n)
        sec = sr.encode(tl, out, log, lambda f, b=base: progress(b + span * f))
        meta = short_meta(topic, group, ss, n, len(parts), sec)
        sr.write_meta(out, meta)
        log("[메타데이터] " + meta["title"])
        if sec > cfg["shorts_target_s"]:
            log(f"  [주의] {sec:.1f}초 — 목표 {cfg['shorts_target_s']}초를 넘었습니다.")
        log(f"[완료] {out} ({sec:.1f}초), 타이밍 로그: {_write_debug(out, sec, tl)}")
        outs.append(out)
    progress(1.0)
    return outs


def render_all_shorts(csv_path, out_dir=ROOT / "output", log=print, progress=lambda f: None):
    """CSV의 모든 그룹을 쇼츠로 렌더링. 쇼츠는 그룹 경계를 넘지 않는다. 반환: [mp4 경로]."""
    groups = list(brick_csv.load_brick_csv(csv_path).groups())
    outs = []
    for n, g in enumerate(groups):
        span = 1 / len(groups)
        outs += render_shorts(csv_path, g, out_dir, log, lambda f, b=n * span: progress(b + span * f))
    return outs


def render_all_long(csv_path, out_dir=ROOT / "output", log=print, progress=lambda f: None):
    """CSV의 모든 그룹을 그룹당 롱폼 하나로 순서대로 렌더링. 반환: [mp4 경로]."""
    groups = list(brick_csv.load_brick_csv(csv_path).groups())
    outs = []
    for n, g in enumerate(groups):
        span = 1 / len(groups)
        out, _, _ = render_long(csv_path, g, out_dir, log, lambda f, b=n * span: progress(b + span * f))
        outs.append(out)
    return outs


def render_long(csv_path, group=None, out_dir=ROOT / "output", log=print, progress=lambda f: None):
    """그룹 하나의 16:9 롱폼 렌더링 + 같은 이름의 메타데이터 json. 반환: (mp4 경로, 길이 초, 타임라인)."""
    topic, group, sentences = _load_group(csv_path, group, log)
    voice_cfg, cfg = load_settings()
    voices = Voices(topic.learn_lang, voice_cfg, log)
    log(f"[브릭] {topic.topic} / {group} / {len(sentences)}문장 / 학습 언어 {topic.learn_lang}")
    tl = build_timeline(topic, group, sentences, voices, cfg, "landscape", shorts=False, log=log)
    progress(0.15)
    out = Path(out_dir) / output_name(topic, group)
    sec = sr.encode(tl, out, log, lambda f: progress(0.15 + 0.85 * f))
    progress(1.0)

    meta, problems = long_meta(topic, group, sentences, tl, sec, out.parent / f"{topic.topic}_섬네일.png")
    sr.write_meta(out, meta)
    log("[메타데이터] " + meta["title"])
    for c in meta["chapters"]:
        log(f"  {c['time']} {c['title']}")
    if problems:
        log(f"  [주의] 유튜브 챕터로 인식되지 않을 수 있음: {', '.join(problems)}")

    log(f"[완료] {out} ({sec:.1f}초), 타이밍 로그: {_write_debug(out, sec, tl)}")
    return out, sec, tl


def main():
    ap = argparse.ArgumentParser(description="브릭 학습 모드 롱폼 / 쇼츠 렌더링")
    ap.add_argument("csv")
    ap.add_argument("--group", help="이 그룹만 렌더링 (기본: CSV의 모든 그룹)")
    ap.add_argument("--format", choices=["long", "shorts", "all"], default="long")
    ap.add_argument("-o", "--out", default=str(ROOT / "output"))
    a = ap.parse_args()
    try:
        if a.format in ("long", "all"):
            render_long(a.csv, a.group, a.out) if a.group else render_all_long(a.csv, a.out)
        if a.format in ("shorts", "all"):
            render_shorts(a.csv, a.group, a.out) if a.group else render_all_shorts(a.csv, a.out)
    except brick_csv.BrickCSVError as e:
        print(e)
        sys.exit(1)


if __name__ == "__main__":
    main()
