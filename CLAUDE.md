# 영어브릭 자동 영상 제작 시스템 — Claude Code 작업 안내

이 폴더는 영어/한국어 학습용 "색상 블록(브릭)" 영상을 만드는 Python 프로젝트다.
작업을 시작하기 전에 아래 문서를 순서대로 읽는다.

1. `docs/01_개발명세.md` — 목표, 확정된 결정사항, 영상 흐름, 화면 규격, 기존 코드 분석
2. `docs/02_블록분류규칙.md` — 영어/한국어 블록 분리와 색상 규칙 (최우선 기준)
3. `docs/03_M3_작업지시.md` — 지금 할 일

## 현재 상태 (2026-10-30 기준)

| 단계 | 내용 | 상태 |
|---|---|---|
| M1 | 새 형식 CSV 읽기·검증 (`brick_csv.py`) | 완료 |
| M2 | 화면 시안 (`brick_frames.py`, `brick_preview.py`, `preview/`) | 완료, 사용자 승인 |
| M3 | 음성 + 타임라인 + 롱폼 렌더링 (`brick_render.py`, 영어학습 영아 그룹) | 완료, 사용자 승인 (2026-10-30) |
| M4 | 전체 그룹 롱폼, 한국어학습용, 메타데이터 json | 완료, 사용자 승인 (2026-10-30) |
| **M5** | **쇼츠 배분과 출력** | **다음 작업** |
| M6 | 런처(GUI) 연결, 설정 항목 | 대기 |

테스트 데이터: `data/아침깨우기_영어학습.csv` (영아 그룹 7문장만, 사용자 확인본), `data/아침깨우기_한국어학습.csv` (영아·유아·초등 21문장)
영어학습 문장 2 `It’s time to wake up,` 분류는 needs_review 상태.

## 반드시 지킬 원칙

1. **기존 파일 보호:** `shadowing_render.py`, `launcher.py`, `uploaders.py`, `thumbnail.py`는 사용자 승인 없이 수정하지 않는다. 새 기능은 `brick_*.py` 새 파일에 만들고, 기존 코드는 import해서 재사용한다. 기존 파일을 고쳐야 하면 먼저 변경 대상·이유·영향 범위를 설명하고 승인을 받는다.
2. **어순 보존:** 블록은 원문에 등장하는 순서대로만 표시·공개한다. 색상 기준으로 재배열하지 않는다.
3. **블록 분류는 `docs/02_블록분류규칙.md`만 따른다.** 애매하면 임의로 확정하지 말고 needs_review로 표시하고 사용자에게 묻는다.
4. **타이밍은 실제 음성 길이 기준.** 고정 시간으로 타임라인을 만들지 않는다.
5. **단계별 보고:** 각 단계가 끝나면 변경한 파일, 변경 이유, 테스트 결과, 남은 문제를 보고하고 승인 후 다음 단계로 간다.
6. **증거로 판정:** "완료"는 실제 실행 결과(렌더링된 영상, 추출한 프레임, 테스트 출력)로 확인한 뒤에만 말한다.
7. **비밀 정보:** `config.json`, `credentials/` 안의 토큰·키를 출력하거나 코드에 하드코딩하지 않는다. 저장소에 올리지 않는다.
8. **업로드:** 이번 단계들에서는 업로드를 실행하지 않는다. 업로드는 항상 비공개(private)가 기본이다.

## 실행 환경

- Windows, Python 3.12+, ffmpeg/ffprobe가 PATH에 있어야 함
- 글꼴: 맑은 고딕(`C:/Windows/Fonts/malgunbd.ttf`, `malgun.ttf`)
- TTS: Edge TTS (`edge-tts`), 캐시는 `build/tts/`
- 의존성: `requirements.txt`

## 자주 쓰는 명령

```bash
python brick_csv.py data/아침깨우기_영어학습.csv            # CSV 검증 + 블록/색상 요약
python brick_preview.py preview data/아침깨우기_영어학습.csv data/아침깨우기_한국어학습.csv   # 화면 시안 PNG
python brick_render.py data/아침깨우기_한국어학습.csv       # 모든 그룹 롱폼 + 메타데이터 json (output/, 타이밍 로그는 build/)
python brick_render.py data/아침깨우기_영어학습.csv --group 영아   # 그룹 하나만
python shadowing_render.py --help                            # 기존(구) 섀도잉 모드 CLI
```

## 처음 할 일

작업 전에 프로젝트 폴더를 git으로 관리하지 않고 있다면 `git init` 후 현재 상태를 첫 커밋으로 남긴다
(`.gitignore`에 `build/`, `output/`, `credentials/`, `config.json`, `__pycache__/` 포함). 문제가 생기면 이 커밋으로 되돌린다.
