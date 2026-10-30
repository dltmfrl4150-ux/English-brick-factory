"""
SNS 업로드 (렌더링과 완전히 분리 — 검수 끝난 파일만 수동으로 업로드)

- YouTube: YouTube Data API v3 (OAuth 데스크톱 앱). 9:16 + 3분 이하 영상은 #Shorts 메타데이터로 Shorts 분류.
- Instagram: Instagram Graph API 릴스 재개형(resumable) 업로드. 로컬 파일을 직접 전송.

각 mp4 옆의 같은 이름 .json(렌더링 시 생성)에서 제목/설명/태그를 읽음.
업로드 전에 이 json을 열어 문구를 검수·수정하면 됨.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

import requests

YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
YOUTUBE_CATEGORY_EDUCATION = "27"


# ───────────────────────── 공통 ─────────────────────────
def load_meta(mp4: Path) -> dict:
    side = Path(mp4).with_suffix(".json")
    if side.exists():
        return json.loads(side.read_text(encoding="utf-8"))
    return {"title": Path(mp4).stem, "description": "", "tags": []}


def probe_video(mp4: Path) -> dict:
    """ffprobe로 가로/세로 크기와 길이 확인 (메타데이터가 없어도 판별 가능하도록)."""
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height:format=duration", "-of", "json", str(mp4)],
        capture_output=True, text=True, check=True, creationflags=flags).stdout
    info = json.loads(out)
    st = info["streams"][0]
    return {"width": st["width"], "height": st["height"], "duration": float(info["format"]["duration"])}


def is_vertical(mp4: Path) -> bool:
    v = probe_video(mp4)
    return v["height"] > v["width"]


# ───────────────────────── YouTube ─────────────────────────
def youtube_service(client_secret: str, token_path: str, log=print):
    """최초 1회 브라우저 로그인 후 token_path에 토큰 저장, 이후 자동 갱신."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    if not client_secret or not Path(client_secret).exists():
        raise FileNotFoundError("YouTube OAuth 클라이언트 JSON(client_secret)을 설정 탭에서 지정하세요.")
    token = Path(token_path)
    creds = None
    if token.exists():
        creds = Credentials.from_authorized_user_file(str(token), YOUTUBE_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            log("[YouTube] 브라우저에서 Google 계정 로그인/권한 승인을 진행하세요.")
            flow = InstalledAppFlow.from_client_secrets_file(client_secret, YOUTUBE_SCOPES)
            creds = flow.run_local_server(port=0)
        token.parent.mkdir(parents=True, exist_ok=True)
        token.write_text(creds.to_json(), encoding="utf-8")
    return build("youtube", "v3", credentials=creds)


def upload_youtube(mp4, cfg: dict, log=print, progress=lambda f: None) -> str:
    """cfg: client_secret, token_path, privacy, made_for_kids. 반환: 영상 URL."""
    from googleapiclient.http import MediaFileUpload

    mp4 = Path(mp4)
    meta = load_meta(mp4)
    video = probe_video(mp4)
    vertical = video["height"] > video["width"]

    title, desc = meta["title"], meta.get("description", "")
    if vertical:
        # Shorts 분류: 세로 + 3분 이하 + #Shorts 표기
        if "#shorts" not in (title + desc).lower():
            desc = f"{desc}\n#Shorts".strip()
        if video["duration"] > 180:
            log(f"  [주의] {video['duration']:.0f}초 — 3분 초과 세로 영상은 Shorts가 아닌 일반 영상으로 분류됩니다.")
    body = {
        "snippet": {
            "title": title[:100],
            "description": desc[:5000],
            "tags": meta.get("tags", []),
            "categoryId": YOUTUBE_CATEGORY_EDUCATION,
        },
        "status": {
            "privacyStatus": cfg.get("privacy", "private"),
            "selfDeclaredMadeForKids": bool(cfg.get("made_for_kids", False)),
        },
    }

    yt = youtube_service(cfg.get("client_secret"), cfg.get("token_path"), log)
    log(f"[YouTube] 업로드 시작 ({'Shorts' if vertical else '일반 영상'}, {body['status']['privacyStatus']}): {mp4.name}")
    media = MediaFileUpload(str(mp4), chunksize=8 * 1024 * 1024, resumable=True, mimetype="video/mp4")
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        status, resp = req.next_chunk()
        if status:
            progress(status.progress())
    progress(1.0)
    video_id = resp["id"]

    thumb = meta.get("thumbnail")
    if not vertical and thumb and Path(thumb).exists():
        try:
            yt.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(thumb)).execute()
            log("  섬네일 적용 완료")
        except Exception as e:  # 채널 인증(전화번호) 전에는 맞춤 섬네일이 거부됨
            log(f"  [주의] 섬네일 적용 실패: {e}")

    url = f"https://youtu.be/{video_id}"
    log(f"[YouTube] 완료: {url}")
    return url


# ───────────────────────── Instagram ─────────────────────────
def upload_instagram(mp4, cfg: dict, log=print, progress=lambda f: None) -> str:
    """cfg: ig_user_id, access_token, api_version. 반환: 게시물 ID.
    흐름: 컨테이너 생성(resumable) -> rupload로 파일 전송 -> 처리 완료 대기 -> 게시."""
    mp4 = Path(mp4)
    user_id, token = cfg.get("ig_user_id"), cfg.get("access_token")
    ver = cfg.get("api_version") or "v21.0"
    if not user_id or not token:
        raise ValueError("Instagram 비즈니스 계정 ID와 액세스 토큰을 설정 탭에서 입력하세요.")
    graph = f"https://graph.facebook.com/{ver}"
    meta = load_meta(mp4)
    caption = "\n\n".join(x for x in [meta["title"].replace(" #Shorts", ""), meta.get("description", "")] if x)
    caption = caption.replace("#Shorts", "#Reels")

    if not is_vertical(mp4):
        log("  [주의] 가로 영상입니다. 릴스 피드에서는 9:16으로 잘려 보입니다.")

    def check(r):
        if not r.ok:
            raise RuntimeError(f"Instagram API 오류 {r.status_code}: {r.text[:300]}")
        return r.json()

    log(f"[Instagram] 컨테이너 생성: {mp4.name}")
    created = check(requests.post(f"{graph}/{user_id}/media", data={
        "media_type": "REELS", "upload_type": "resumable",
        "caption": caption[:2200], "share_to_feed": "true", "access_token": token,
    }, timeout=60))
    container, upload_uri = created["id"], created["uri"]

    size = mp4.stat().st_size
    log(f"[Instagram] 파일 전송 ({size / 1e6:.1f}MB)")
    with open(mp4, "rb") as f:
        check(requests.post(upload_uri, data=f, timeout=1800, headers={
            "Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size),
        }))
    progress(0.6)

    log("[Instagram] 서버 처리 대기")
    for _ in range(120):  # 최대 10분
        st = check(requests.get(f"{graph}/{container}", params={
            "fields": "status_code,status", "access_token": token}, timeout=60))
        code = st.get("status_code")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram 처리 실패: {st.get('status')}")
        time.sleep(5)
    else:
        raise TimeoutError("Instagram 처리 대기 시간 초과")
    progress(0.9)

    pub = check(requests.post(f"{graph}/{user_id}/media_publish", data={
        "creation_id": container, "access_token": token}, timeout=60))
    progress(1.0)
    log(f"[Instagram] 게시 완료: media id {pub['id']}")
    return pub["id"]
