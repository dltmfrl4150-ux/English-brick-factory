"""
영어브릭 섀도잉 영상 런처 (tkinter)

[제작] 탭: CSV 선택 / TTS 음성 / 16:9 롱폼 · 9:16 숏폼 3종 렌더링 / 섬네일 / 검수 후 업로드
[계정 · API 설정] 탭: YouTube OAuth, Instagram Graph API 토큰
하단: 진행 바 + 실시간 로그

렌더링과 업로드는 완전히 분리됨 (Option B: 검수 후 안전 업로드).
업로드는 출력 폴더에서 직접 고른 파일만, 확인 창을 거쳐 실행.

실행: python launcher.py
"""
import json
import os
import queue
import threading
import traceback
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import shadowing_render as render
import thumbnail
import uploaders

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"

DEFAULT_CONFIG = {
    "csv_path": str(render.DEFAULT_CSV),
    "output_dir": str(render.DEFAULT_OUT),
    "voices": dict(render.DEFAULT_VOICES),
    "shorts_compact": True,
    "youtube": {
        "client_secret": "",
        "token_path": str(ROOT / "credentials" / "youtube_token.json"),
        "privacy": "private",
        "made_for_kids": False,
    },
    "instagram": {"ig_user_id": "", "access_token": "", "api_version": "v21.0"},
}

VOICE_LABELS = [("en_mom", "영어 엄마"), ("en_kid", "영어 아이"),
                ("ko_mom", "한국어 엄마"), ("ko_kid", "한국어 아이")]
VOICE_CHOICES = {
    "en": ["en-US-JennyNeural", "en-US-AnaNeural", "en-US-AriaNeural", "en-US-AvaNeural",
           "en-US-EmmaNeural", "en-US-MichelleNeural", "en-US-AvaMultilingualNeural"],
    "ko": ["ko-KR-SunHiNeural", "ko-KR-InJoonNeural", "ko-KR-HyunsuMultilingualNeural"],
}


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if CONFIG_PATH.exists():
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            for k, v in saved.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except (json.JSONDecodeError, OSError):
            pass
    return cfg


def save_config(cfg: dict):
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def open_path(path):
    os.startfile(str(path))  # Windows 기본 프로그램/탐색기로 열기


class Launcher(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("영어브릭 섀도잉 영상 런처")
        self.geometry("980x800")
        self.minsize(860, 680)

        self.cfg = load_config()
        save_config(self.cfg)            # 첫 실행 시 기본값으로 config.json 생성
        self.events = queue.Queue()      # 작업 스레드 -> UI (log/progress/done)
        self.busy = False
        self.action_buttons = []

        self.vars = {}
        self._build_ui()
        self.refresh_files()
        self.after(100, self._drain_events)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ───────── 설정 변수 (변경 즉시 config.json 저장) ─────────
    def var(self, path, kind=tk.StringVar):
        """'youtube.privacy' 같은 경로로 cfg와 연결된 tk 변수."""
        keys = path.split(".")
        node = self.cfg
        for k in keys[:-1]:
            node = node[k]
        v = kind(value=node[keys[-1]])

        def on_change(*_):
            try:
                node[keys[-1]] = v.get()
            except tk.TclError:
                return
            save_config(self.cfg)
        v.trace_add("write", on_change)
        self.vars[path] = v
        return v

    # ───────── UI 구성 ─────────
    def _build_ui(self):
        style = ttk.Style(self)
        style.configure("Big.TButton", padding=(10, 8))

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=10, pady=(10, 4))
        make = ttk.Frame(nb, padding=8)
        account = ttk.Frame(nb, padding=8)
        nb.add(make, text="  제작  ")
        nb.add(account, text="  계정 · API 설정  ")
        self._build_make_tab(make)
        self._build_account_tab(account)

        # 하단: 진행 바 + 로그 (탭과 무관하게 항상 표시)
        bottom = ttk.Frame(self, padding=(10, 0, 10, 10))
        bottom.pack(fill="both")
        row = ttk.Frame(bottom)
        row.pack(fill="x", pady=(0, 4))
        self.status = tk.StringVar(value="대기 중")
        ttk.Label(row, textvariable=self.status).pack(side="left")
        self.pct = tk.StringVar(value="")
        ttk.Label(row, textvariable=self.pct).pack(side="right")
        self.progress = ttk.Progressbar(bottom, maximum=1000)
        self.progress.pack(fill="x")
        self.log_box = ScrolledText(bottom, height=11, state="disabled", font=("Consolas", 9))
        self.log_box.pack(fill="both", expand=True, pady=(6, 0))

    def _build_make_tab(self, parent):
        # 1. 대본 및 파일
        f = ttk.LabelFrame(parent, text="대본 및 파일", padding=8)
        f.pack(fill="x")
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="CSV 대본").grid(row=0, column=0, sticky="w")
        ttk.Entry(f, textvariable=self.var("csv_path")).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(f, text="CSV 선택…", command=self.pick_csv).grid(row=0, column=2)
        ttk.Label(f, text="출력 폴더").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(f, textvariable=self.var("output_dir")).grid(row=1, column=1, sticky="ew", padx=6, pady=(6, 0))
        btns = ttk.Frame(f)
        btns.grid(row=1, column=2, pady=(6, 0))
        ttk.Button(btns, text="변경…", command=self.pick_out_dir).pack(side="left")
        ttk.Button(btns, text="출력 폴더 열기", command=self.open_out_dir).pack(side="left", padx=(4, 0))

        # 2. TTS 음성
        f = ttk.LabelFrame(parent, text="TTS 음성 (Edge TTS · 변경 시 자동 저장)", padding=8)
        f.pack(fill="x", pady=(8, 0))
        self.voice_boxes = {}
        for i, (key, label) in enumerate(VOICE_LABELS):
            r, c = divmod(i, 2)
            ttk.Label(f, text=label).grid(row=r, column=c * 2, sticky="w", padx=(0 if c == 0 else 16, 6), pady=2)
            cb = ttk.Combobox(f, textvariable=self.var(f"voices.{key}"), width=34,
                              values=VOICE_CHOICES[key[:2]])
            cb.grid(row=r, column=c * 2 + 1, sticky="w", pady=2)
            self.voice_boxes[key] = cb
        ttk.Button(f, text="전체 음성 목록 불러오기", command=self.load_voice_list).grid(
            row=2, column=0, columnspan=4, sticky="w", pady=(6, 0))

        # 3. 렌더링
        f = ttk.LabelFrame(parent, text="영상 렌더링 (CSV 1개 기준)", padding=8)
        f.pack(fill="x", pady=(8, 0))
        btn_row = ttk.Frame(f)
        btn_row.pack(fill="x")
        for text, cmd in [("① 16:9 롱폼 렌더링", self.do_render_long),
                          ("② 9:16 숏폼 3종 렌더링", self.do_render_shorts),
                          ("⑤ 롱폼 섬네일 생성", self.do_thumbnail)]:
            b = ttk.Button(btn_row, text=text, style="Big.TButton", command=cmd)
            b.pack(side="left", expand=True, fill="x", padx=3)
            self.action_buttons.append(b)
        ttk.Checkbutton(f, variable=self.var("shorts_compact", tk.BooleanVar), text=(
            "숏폼 3분 압축 모드 (YouTube Shorts용) — 모든 Phase 유지, Phase 2·3 문장당 1회, "
            "안내 카드 생략, 여백 축소, 필요 시 대기 배수 자동 조정(최소 1.5배)")).pack(
            anchor="w", padx=3, pady=(6, 0))

        # 4. 업로드 (렌더링과 분리)
        f = ttk.LabelFrame(parent, text="SNS 업로드 — 출력 폴더에서 검수한 파일만 선택해서 업로드", padding=8)
        f.pack(fill="both", expand=True, pady=(8, 0))
        top = ttk.Frame(f)
        top.pack(fill="x")
        ttk.Label(top, text="파일을 재생·메타데이터(json)로 검수한 뒤 선택하세요 (Ctrl/Shift 다중 선택)").pack(side="left")
        ttk.Button(top, text="새로고침", command=self.refresh_files).pack(side="right")
        ttk.Button(top, text="메타데이터 편집", command=self.open_selected_meta).pack(side="right", padx=4)
        ttk.Button(top, text="선택 재생", command=self.play_selected).pack(side="right")

        self.file_list = tk.Listbox(f, selectmode="extended", height=6, font=("Malgun Gothic", 9))
        self.file_list.pack(fill="both", expand=True, pady=6)
        self.files = []

        row = ttk.Frame(f)
        row.pack(fill="x")
        for text, cmd in [("③ YouTube 업로드", self.do_upload_youtube),
                          ("④ Instagram 릴스 업로드", self.do_upload_instagram)]:
            b = ttk.Button(row, text=text, style="Big.TButton", command=cmd)
            b.pack(side="left", expand=True, fill="x", padx=3)
            self.action_buttons.append(b)

    def _build_account_tab(self, parent):
        f = ttk.LabelFrame(parent, text="YouTube Data API v3 (OAuth 데스크톱 앱)", padding=10)
        f.pack(fill="x")
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="OAuth 클라이언트 JSON").grid(row=0, column=0, sticky="w")
        ttk.Entry(f, textvariable=self.var("youtube.client_secret")).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(f, text="찾기…", command=lambda: self.pick_file("youtube.client_secret")).grid(row=0, column=2)
        ttk.Label(f, text="토큰 저장 경로").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(f, textvariable=self.var("youtube.token_path")).grid(row=1, column=1, sticky="ew", padx=6, pady=(6, 0))
        ttk.Label(f, text="공개 상태").grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Combobox(f, textvariable=self.var("youtube.privacy"), state="readonly", width=12,
                     values=["private", "unlisted", "public"]).grid(row=2, column=1, sticky="w", padx=6, pady=(6, 0))
        ttk.Checkbutton(f, text="아동용 콘텐츠(Made for Kids)로 신고",
                        variable=self.var("youtube.made_for_kids", tk.BooleanVar)).grid(
            row=3, column=1, sticky="w", padx=6, pady=(6, 0))
        ttk.Label(f, foreground="#666", wraplength=820, justify="left", text=(
            "Google Cloud Console에서 YouTube Data API v3 사용 설정 → OAuth 클라이언트 ID(데스크톱 앱) 생성 → JSON 다운로드. "
            "첫 업로드 때 브라우저 로그인 창이 뜨고, 이후에는 토큰으로 자동 로그인합니다. "
            "기본 공개 상태 'private'로 올린 뒤 YouTube Studio에서 확인하고 공개하는 것을 권장합니다.")).grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(10, 0))

        f = ttk.LabelFrame(parent, text="Instagram Graph API (비즈니스/크리에이터 계정)", padding=10)
        f.pack(fill="x", pady=(10, 0))
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="IG 사용자 ID").grid(row=0, column=0, sticky="w")
        ttk.Entry(f, textvariable=self.var("instagram.ig_user_id")).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Label(f, text="액세스 토큰").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(f, textvariable=self.var("instagram.access_token"), show="•").grid(
            row=1, column=1, sticky="ew", padx=6, pady=(6, 0))
        ttk.Label(f, text="Graph API 버전").grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(f, textvariable=self.var("instagram.api_version"), width=12).grid(
            row=2, column=1, sticky="w", padx=6, pady=(6, 0))
        ttk.Label(f, foreground="#666", wraplength=820, justify="left", text=(
            "Facebook 페이지와 연결된 Instagram 비즈니스/크리에이터 계정이 필요합니다. "
            "Meta 앱에 instagram_basic, instagram_content_publish 권한을 받은 장기 액세스 토큰을 입력하세요.")).grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(10, 0))

        ttk.Label(parent, foreground="#B00", wraplength=860, justify="left", text=(
            f"※ 키와 토큰은 {CONFIG_PATH.name} / credentials 폴더에 평문으로 저장됩니다. "
            "이 폴더를 공유하거나 저장소에 올리지 마세요.")).pack(anchor="w", pady=(12, 0))

    # ───────── 파일 선택 ─────────
    def pick_csv(self):
        p = filedialog.askopenfilename(title="CSV 대본 선택", filetypes=[("CSV", "*.csv")],
                                       initialdir=Path(self.vars["csv_path"].get()).parent)
        if p:
            self.vars["csv_path"].set(p)

    def pick_out_dir(self):
        p = filedialog.askdirectory(title="출력 폴더 선택", initialdir=self.vars["output_dir"].get())
        if p:
            self.vars["output_dir"].set(p)
            self.refresh_files()

    def pick_file(self, key):
        p = filedialog.askopenfilename(filetypes=[("JSON", "*.json"), ("모든 파일", "*.*")])
        if p:
            self.vars[key].set(p)

    def out_dir(self) -> Path:
        d = Path(self.vars["output_dir"].get())
        d.mkdir(parents=True, exist_ok=True)
        return d

    def open_out_dir(self):
        open_path(self.out_dir())

    def refresh_files(self):
        self.file_list.delete(0, "end")
        self.files = sorted(self.out_dir().glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
        for p in self.files:
            meta = uploaders.load_meta(p)
            kind = {"long": "16:9 롱폼", "short": "9:16 숏폼"}.get(meta.get("kind"), "?")
            dur = meta.get("duration_sec")
            dur = f"{int(dur // 60)}:{int(dur % 60):02d}" if dur else "-"
            self.file_list.insert("end", f"[{kind}] {p.name}   ({dur})")

    def selected_files(self):
        return [self.files[i] for i in self.file_list.curselection()]

    def play_selected(self):
        for p in self.selected_files()[:1]:
            open_path(p)

    def open_selected_meta(self):
        for p in self.selected_files()[:1]:
            side = p.with_suffix(".json")
            if not side.exists():
                side.write_text(json.dumps(uploaders.load_meta(p), ensure_ascii=False, indent=2), encoding="utf-8")
            open_path(side)

    def load_voice_list(self):
        def job(log, progress):
            import asyncio
            import edge_tts
            voices = asyncio.run(edge_tts.list_voices())
            found = {lang: sorted(v["ShortName"] for v in voices if v["Locale"].startswith(lang))
                     for lang in ("en", "ko")}
            self.after(0, lambda: [cb.configure(values=found[k[:2]]) for k, cb in self.voice_boxes.items()])
            log(f"음성 목록: 영어 {len(found['en'])}개, 한국어 {len(found['ko'])}개")
        self.run_job("음성 목록 불러오기", job)

    # ───────── 작업 실행 (백그라운드 스레드) ─────────
    def run_job(self, name, func, on_done=None):
        if self.busy:
            messagebox.showinfo("작업 중", "다른 작업이 진행 중입니다. 끝난 뒤 다시 시도하세요.")
            return
        self.busy = True
        for b in self.action_buttons:
            b.state(["disabled"])
        self.status.set(f"{name} 진행 중…")
        self.progress["value"] = 0
        self.pct.set("0%")
        self.log(f"===== {name} 시작 =====")

        def log(msg):
            self.events.put(("log", str(msg)))

        def progress(f):
            self.events.put(("progress", f))

        def worker():
            try:
                result = func(log, progress)
                self.events.put(("done", (name, True, result, on_done)))
            except Exception as e:
                log(traceback.format_exc())
                self.events.put(("done", (name, False, e, on_done)))
        threading.Thread(target=worker, daemon=True).start()

    def _drain_events(self):
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "log":
                    self.log(data)
                elif kind == "progress":
                    self.progress["value"] = max(0, min(1, data)) * 1000
                    self.pct.set(f"{data * 100:.0f}%")
                elif kind == "done":
                    self._finish(*data)
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _finish(self, name, ok, result, on_done):
        self.busy = False
        for b in self.action_buttons:
            b.state(["!disabled"])
        if ok:
            self.progress["value"] = 1000
            self.pct.set("100%")
            self.status.set(f"{name} 완료")
            self.log(f"===== {name} 완료 =====")
            if on_done:
                on_done(result)
        else:
            self.status.set(f"{name} 실패")
            self.log(f"===== {name} 실패: {result} =====")
            messagebox.showerror(f"{name} 실패", str(result))
        self.refresh_files()

    def log(self, msg):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", msg + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    # ───────── 버튼 동작 ─────────
    def csv_path(self):
        p = Path(self.vars["csv_path"].get())
        if not p.exists():
            messagebox.showerror("CSV 없음", f"CSV 파일을 찾을 수 없습니다:\n{p}")
            return None
        return p

    def do_render_long(self):
        csv = self.csv_path()
        if csv:
            out, voices = self.out_dir(), dict(self.cfg["voices"])
            self.run_job("16:9 롱폼 렌더링",
                         lambda log, progress: render.render_long(csv, out, voices, log, progress))

    def do_render_shorts(self):
        csv = self.csv_path()
        if csv:
            out, voices = self.out_dir(), dict(self.cfg["voices"])
            compact = bool(self.cfg["shorts_compact"])
            self.run_job("9:16 숏폼 3종 렌더링" + (" (3분 압축)" if compact else ""),
                         lambda log, progress: render.render_shorts(csv, out, voices, log, progress, compact))

    def do_thumbnail(self):
        csv = self.csv_path()
        if not csv:
            return

        def job(log, progress):
            topic = render.load_topic(csv)
            png = thumbnail.make_thumbnail(csv, self.out_dir() / f"{topic}_섬네일.png")
            log(f"[섬네일] {png}")
            return png
        self.run_job("롱폼 섬네일 생성", job, on_done=open_path)

    def confirm_upload(self, platform, files, extra=""):
        if not files:
            messagebox.showwarning("파일 선택", "업로드할 영상을 목록에서 먼저 선택하세요.")
            return False
        names = "\n".join(f"• {p.name}" for p in files)
        return messagebox.askyesno(
            f"{platform} 업로드 확인",
            f"아래 {len(files)}개 파일을 {platform}에 업로드합니다.\n\n{names}\n\n{extra}"
            "검수(재생·제목 확인)를 마쳤나요?", icon="warning")

    def do_upload_youtube(self):
        files = self.selected_files()
        yt = dict(self.cfg["youtube"])
        if not self.confirm_upload("YouTube", files, f"공개 상태: {yt['privacy']}\n"):
            return

        def job(log, progress):
            urls = []
            for n, p in enumerate(files):
                urls.append(uploaders.upload_youtube(
                    p, yt, log, lambda f, n=n: progress((n + f) / len(files))))
            return urls
        self.run_job("YouTube 업로드", job)

    def do_upload_instagram(self):
        files = self.selected_files()
        ig = dict(self.cfg["instagram"])
        if not self.confirm_upload("Instagram 릴스", files, "릴스는 바로 공개 게시됩니다.\n"):
            return

        def job(log, progress):
            ids = []
            for n, p in enumerate(files):
                ids.append(uploaders.upload_instagram(
                    p, ig, log, lambda f, n=n: progress((n + f) / len(files))))
            return ids
        self.run_job("Instagram 업로드", job)

    def _on_close(self):
        if self.busy and not messagebox.askyesno("종료", "작업이 진행 중입니다. 그래도 종료할까요?"):
            return
        save_config(self.cfg)
        self.destroy()


if __name__ == "__main__":
    Launcher().mainloop()
