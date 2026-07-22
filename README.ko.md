# wikimap

[![ci](https://github.com/dhha22/wikimap/actions/workflows/ci.yml/badge.svg)](https://github.com/dhha22/wikimap/actions/workflows/ci.yml) [![PyPI](https://img.shields.io/pypi/v/wikimap)](https://pypi.org/project/wikimap/) [![Python](https://img.shields.io/badge/python-3.8%2B-blue)](https://pypi.org/project/wikimap/) [![license](https://img.shields.io/github/license/dhha22/wikimap)](LICENSE)

[English](README.md) | 한국어

**지식 vault를 위한 zero-LLM 증분 인덱스 + 지연 시맨틱 레이어 — 마크다운, HTML, PDF, 이미지.**

파이썬 파일 하나. 의존성 0. 빌드 시점 LLM 비용 0 — 언제나. 인덱스가 아무리 오래 방치돼도 업데이트는 1초 미만.

지식 vault(Obsidian vault, 팀 위키, 스펙·슬라이드·계획 문서 폴더)를 다루는 AI 코딩 어시스턴트(Claude Code 등)를 위해 만들어졌습니다.

## 왜 지식 그래프 도구나 RAG가 아닌가?

> **비교 대상인 [graphify](https://github.com/Graphify-Labs/graphify)란?** 문서를 LLM에 통과시켜 "개념"과 "개념 사이의 관계"를 뽑아 지식 그래프를 만드는 도구입니다. 이 README에서 계속 비교 대상으로 삼습니다.

graphify 같은 지식 그래프 도구도, RAG도 **미리** 다 계산해 둡니다. 코퍼스 전체를 LLM에 통과시켜 그래프나 벡터 스토어를 만들어 두는 방식이죠. 잘 작동하지만, **문서를 고칠 때마다 그 비용을 다시 냅니다.** 하나만 고쳐도 재추출이 돌고, 일주일 방치하면 "증분" 업데이트가 사실상 절반을 다시 처리합니다.

wikimap은 순서를 뒤집습니다: **구조는 지금 파싱하고, 의미는 물어볼 때 배웁니다.**

- **구조는 공짜입니다.** 제목·헤딩·링크·요구사항 ID는 그냥 파싱하면 나옵니다. LLM도, API 키도, 관리할 임베딩도 없습니다.
- **의미는 물어볼 때 쌓입니다.** 에이전트가 답을 찾아내면 그 답을 저장하고, 두 문서가 관련 있다고 확인하면 그 링크를 저장합니다. 쓸지 안 쓸지 모를 걸 미리 계산해 두지 않습니다.

이게 안전한 이유는 **저장되는 모든 것에 원본 파일의 해시가 함께 찍히기 때문**입니다. 파일이 바뀌면 낡은 답은 자동으로 빠집니다. 에이전트에게 철 지난 사실을 슬쩍 먹이는 일이 없습니다.

그래서 LLM 비용은 **물어본 만큼만** 나갑니다. vault가 커져도 늘지 않습니다.

## 왜 wikimap인가

- **즉각적이고 공짜인 인덱싱.** 전체 빌드도 증분 갱신도 1초 안에 끝나고 **LLM 토큰이 0**입니다 — graphify는 같은 vault에 수 분·수백만 토큰이 듭니다.
- **더 정확한 검색.** 블라인드 135질의(코퍼스만 읽은 에이전트가 출제, 질의 실행 전에 검증)에서 wikimap은 새로 빌드한 graphify 그래프를 recall로 앞서고, 스니펫에 정답 줄까지 담아 보여줍니다.
- **결정적이고 자가 정리.** 같은 입력 → 바이트 단위 동일 인덱스, 삭제된 파일은 알아서 사라집니다. 고스트 노드도, 실행마다 뒤섞이는 결과도 없습니다.
- **언어·포맷 무관.** 하드코딩된 불용어 목록이 없어 한/영/혼합이 모두 되고, Markdown 외 `.txt/.rst/.org/.adoc`·HTML·PDF·이미지 파일명까지 인덱싱합니다.

| | wikimap | graphify |
|---|---|---|
| 전체 인덱스 빌드 | **1초 미만, $0** | 수 분 + LLM 비용 |
| 수정 후 업데이트 | **~0.07초, 0 토큰** | ~95초 + 수만 토큰 |
| 검색 정확도 (recall@5, 블라인드 135질의) | **0.83** | 0.57 |
| 스니펫에 정답 줄 표시 | 예 (섹션 + 라인) | 아니오 (엔티티 라벨만) |
| 결정성 | 실행마다 바이트 동일 | 비결정적 그래프 |

<sub>한/영 vault(M 시리즈 Mac) 실측입니다. 전체 방법론과 사전 등록 블라인드 벤치마크는 [changelog](CHANGELOG.md)에 있습니다. 테스트 136건(stdlib만), macOS·Linux·Windows / Python 3.8~3.13.</sub>

**어려운 질의는 fan-out.** 질문에 다른 표현 1~2개를 얹어 한 번에 넘기면 랭킹이 합쳐져, 여러 표현이 공통으로 지목한 문서가 위로 올라옵니다:

```bash
wikimap search "세션 얼마나 유지돼?" "세션 만료" "REQ-02 타임아웃"
```

표현은 에이전트가 알아서 쓰고(추가 API 호출 없음), 출력의 `weak: true`가 언제 재작성이 필요한지 알려줍니다 — 쉬운 질의는 그대로 정확하게, 재작성은 어휘가 비는 곳에만 씁니다.

## 설치

```bash
pipx install wikimap                # 또는: uv tool install wikimap / pip install wikimap
cd your-vault && wikimap update
```

또는 파일 하나만 복사 — 같은 결과, 오프라인·pip 없는 환경에서도 동작:

```bash
curl -O https://raw.githubusercontent.com/dhha22/wikimap/main/wikimap.py
cd your-vault && python3 wikimap.py update
```

어느 쪽이든 `wikimap install`(또는 `python3 wikimap.py install`)이 AI 에이전트들에 등록해 줍니다 — 아래 참조. Python 3.8+ 외에는 아무것도 필요 없습니다.

## 어떤 AI 에이전트와도 사용

wikimap은 특정 어시스턴트에 종속되지 않습니다. 코어는 평범한 CLI(모든 질의 명령에 `--json`)이고, 등록은 오픈 표준을 따릅니다:

- **Claude Code, Codex, GitHub Copilot 등 [agent-skills](https://agentskills.io) 지원 도구** — `wikimap install`이 스킬(`SKILL.md` + 도구 본체)을 `~/.claude/skills/wikimap/`(Claude Code)과 `~/.agents/skills/wikimap/`(Codex 등이 스캔하는 오픈 표준 경로) 두 곳에 복사합니다. 에이전트가 자동 발견해서 vault 질문에 wikimap을 꺼내 씁니다. 한 곳만 원하면 `--target claude|agents`.
- **repo 단위 팀 공유** — `wikimap install --project`는 `./.claude` + `./.agents`에 설치. 커밋하면 팀원 전원의 에이전트가 같은 설정을 받습니다.
- **Cursor 등 `AGENTS.md`를 읽는 도구** — `wikimap install --agents-md`가 `./AGENTS.md`에 마커로 구분된 사용 규칙 블록을 삽입합니다 (멱등: 재실행하면 블록만 갱신되고 나머지 내용은 절대 건드리지 않음).
- **그 외 전부** — 셸 명령을 실행할 수 있는 에이전트라면 `wikimap search/links/path/suggest ... --json`을 직접 쓰면 됩니다. 스킬 파일은 사용 설명서일 뿐 런타임 의존성이 아닙니다.

**스킬은 두 개가 설치되고**, 에이전트가 알아서 골라 씁니다:

| 스킬 | 에이전트가 꺼내 쓰는 상황 |
|---|---|
| `wikimap` | vault에 대해 질문하거나, vault를 수정해서 재인덱싱이 필요할 때 |
| `graphify-to-wikimap` | `graphify-out/` 디렉터리를 발견했고 graphify를 걷어내려 할 때 — `wikimap migrate`를 실행한 뒤, 명령어가 손댈 수 없는 운영 규칙과 git 설정까지 정리합니다 |

마음껏 커스터마이즈하세요: 설치된 `SKILL.md`에 vault 경로·언어·자기 규칙을 적어도 — 업그레이드는 기존 `SKILL.md`를 절대 덮어쓰지 않고 도구 본체만 갱신합니다. **두 스킬 다** 그렇게 보존되고, 테스트로 게이트되어 있습니다.

## 실제 모습

```console
$ wikimap update
wikimap: 304 files indexed (2 changed, 0 deleted) in 147ms | skipped 2 non-indexed files (.tsv 2) | notes: 3 fresh, 0 stale | edges: 112 fresh, 2 stale | MAP.md updated

$ wikimap search "세션 만료 정책"
[NOTE fresh 2026-07-02] Q: 세션은 얼마나 유지되나?
  30분 슬라이딩 만료; 리프레시 토큰은 14일 (REQ-02)
  sources: specs/auth-spec.md
specs/auth-spec.md:12  [로그인 정책]  (score 27)
  REQ-01 세션 만료는 30분. [[auth-plan]] 참고.
```

결과마다 **파일·라인 번호·매치된 줄**이 나옵니다. 에이전트가 파일을 통째로 다시 읽지 않고 해당 섹션으로 바로 갑니다. 맨 위 `[NOTE fresh]`는 예전에 저장해 둔 답변인데, **원본이 그때 그대로일 때만** 보여줍니다.

## 명령어

실제로 치게 될 두 개:

| 명령어 | 하는 일 |
|---|---|
| `update` | 바뀐 것만 재인덱스하고 `MAP.md` 갱신. 1초 미만, $0. 수정 후 실행하거나 git 훅에 맡기세요 |
| `search "질의" ["재작성" ...]` | 질문에 답하는 섹션 찾기. 파일·라인 번호·매치된 줄을 돌려줍니다(상위 결과는 ±2줄 컨텍스트 동봉, `--compact`는 결과당 한 줄). 표현을 더 넘기면 하나의 랭킹으로 융합 — 그럴 가치가 있는 순간은 JSON의 `weak: true`가 알려줍니다 |

나머지는 용도별로:

| | 명령어 | 하는 일 |
|---|---|---|
| **연결 따라가기** | `links <문서>` | 이 문서가 뭘 가리키고 뭐가 이걸 가리키는지 — `REQ-nn` ID를 언급하는 문서까지. 각 항목이 **사람이 쓴 링크인지 에이전트가 추론한 건지** 표시됩니다 |
| | `path <a> <b>` | 두 문서를 잇는 최단 링크 사슬 |
| **연결 늘리기** | `suggest` | *있어야 할* 링크를 공짜 신호(공유 희귀어, 같은 요구사항 ID, 폴더 근접성)로 제안. 1초 미만, LLM 없음 |
| | `link add <문서> <대상>` | 확정된 링크를 문서 본문에 기록. `--apply` 없으면 dry run |
| **답변 기억하기** | `note add` | 에이전트가 알아낸 답을 출처에 고정해 저장 |
| | `edge add` / `edge repin` | 두 문서의 연결 확정 / 수정 후 재고정 |
| | `notes` / `edges` | 캐시된 것 목록 — stale은 알아서 숨습니다 |
| **시맨틱 검색** | `embed set` / `semsearch` | 문서와 **단어가 하나도 안 겹치는** 질문용. 벡터는 에이전트가 만들고(어떤 모델이든), wikimap은 저장·랭킹만 |
| **관리** | `mv <old> <new>` | 문서 개명 + 그걸 가리키는 모든 링크 재작성 |
| | `fix-links` | 깨진 링크의 대상 후보 제안 (자동 적용 안 함) |
| | `doctor` | 읽기 전용 무결성 점검: 인덱스 신선도·semantics 유효성·깨진 링크·stale 핀을 한 번에 판정 |
| | `install` | 에이전트 스킬로 등록, `--hook`이면 커밋마다 자동 `update` |
| | `migrate` | graphify vault를 한 명령어로 이관 (아래 참고). `--apply` 없으면 dry run |

캐시되는 것(노트·엣지·임베딩)은 전부 **원본 파일의 콘텐츠 해시에 고정**됩니다. 그 파일을 고치면 캐시된 지식은 알아서 stale이 되어 빠집니다 — 에이전트에게 낡은 사실을 먹이는 대신에요.

모든 질의 명령어는 `--json`을 받습니다. 전체 플래그(구절·필드·유형 필터, 컨텍스트 줄, ignore 규칙 등)는 `wikimap <명령어> --help`로 보세요.

### graphify에서 넘어오시나요?

```bash
wikimap migrate            # 뭘 할지 정확히 보여줍니다
wikimap migrate --apply    # 실행합니다
```

graphify가 추론한 연결을 가져오고, 아티팩트(`graphify-out/`, `.graphifyignore`)를 지우고, 재인덱싱까지 한 번에 합니다. **문서는 절대 건드리지 않습니다** — 당신이 쓴 `graphify-회고.md`는 아티팩트가 아니라 내 글이니 그대로 둡니다.

**순서가 중요한데 이 명령어가 알아서 지킵니다: `graph.json`을 지우기 전에 엣지를 먼저 가져옵니다.** 손으로 하다 순서를 뒤집으면 그 연결은 영영 못 되찾습니다. 가져온 엣지는 오히려 **더 좋아집니다** — 양쪽 문서의 해시에 묶여서, 문서가 바뀌면 알아서 빠집니다. graphify 그래프엔 없던 보장입니다.

엣지를 버리고 새로 시작하려면 `--apply --no-import`를 쓰세요. 아니면 에이전트에게 **"이 vault를 graphify에서 떼어내줘"**라고만 해도 됩니다 — `graphify-to-wikimap` 스킬이 함께 깔려서, 명령어 실행은 물론 명령어가 못 하는 것(`CLAUDE.md`·`AGENTS.md` 규칙 교체, git 추적 해제)까지 처리합니다.

## LLM 없이 연결을 찾아내는 방식

1. **`suggest`가 공짜로 후보를 제안합니다.** 희귀한 용어를 공유하거나, 같은 요구사항 ID를 인용하거나, 그냥 같은 폴더에 있는 두 문서는 관련 있을 가능성이 큽니다. **당신이 이미 만들어 둔 폴더 구조가 곧 공짜 시맨틱**이고, 이걸 알아채는 데 LLM은 필요 없습니다.
2. **에이전트는 후보만 판별하고**, 진짜만 `link add`로 문서에 씁니다. 코퍼스 전체가 아니라 짧은 후보 목록만 읽으므로 **비용은 vault 크기가 아니라 수정량에 비례**합니다.
3. **확정된 링크는 알아서 stale이 됩니다** — 어느 한쪽이 바뀌면요. 수정 후에도 여전히 유효하다면 `edge repin`으로 rationale을 다시 타이핑하지 않고 유지합니다.

**링크가 하나도 없는 폴더에서 시작한다면?** `suggest -n 0 --json`으로 후보를 뽑고, 에이전트에게 판별시키고, 진짜만 `link add`로 적용하세요.

이건 어렵게 검증했습니다: 348문서 vault에서 **사람이 쓴 위키링크 949개를 전부 제거하고** 복원을 시도했습니다. 후보 전수 조사는 1초도 안 걸리고, **원래 링크의 85%를 되찾습니다** — 그동안 LLM은 후보 쌍만 볼 뿐 코퍼스는 건드리지 않습니다.

## 산출물

- `MAP.md` — vault 루트. 디렉터리 분류, 허브 문서, 최근 변경, 문서 횡단 요구사항 ID, 추론 연결, fresh 노트. 에이전트의 진입점.
- `.wikimap/semantics.jsonl` — 노트와 엣지 본체, append-only JSON lines. **이 파일이 시맨틱 레이어의 원본(source of truth)** 입니다: git에 커밋해 어시스턴트가 vault에 대해 학습한 것을 백업·공유하세요. 손으로 편집 가능하며, 잘못된 한 줄이 레이어 전체를 무너뜨리지 않습니다.
- `.wikimap/index.db` — SQLite. 파생 캐시라 정말로 지워도 됩니다: 언제든 삭제하면 `update`가 파일들 + `semantics.jsonl`에서 손실 없이 재구축합니다.

≤0.5.x에서 업그레이드: 첫 실행이 기존 DB의 노트/엣지를 `semantics.jsonl`로 자동 이관합니다. 1회성, 할 일 없음.

## 다른 vault 도구와의 공존

Obsidian이나 정적 사이트 생성기처럼 **같은 폴더를 보는 다른 도구**가 있어도 됩니다. 서로 안 밟게 하는 방법 세 가지:

- **`.wikimapignore`** — 다른 도구의 산출물(휴지통, 빌드 출력)을 인덱스에서 뺍니다. `.trash/`, `.obsidian/`는 기본으로 제외됩니다.
- **`--map-path .wikimap/MAP.md`** — 다른 도구가 루트의 마크다운을 훑는다면, 루트에 생긴 `MAP.md`가 거대한 허브 노드로 잡혀 그쪽 그래프를 망칩니다. `.wikimap/` 안으로 옮기면 에이전트만 보게 됩니다. 아예 안 만들려면 `--no-map`.
- **`suggest --wikilink`** — 연결을 확정할 땐 `edge add`보다 문서 본문에 `[[링크]]`를 직접 넣는 쪽이 낫습니다. **명시적 링크는 모든 vault 도구가 알아듣는 유일한 형식**이니까요.

## 범위

wikimap의 목표는 **폴더 안 모든 문서가 — 포맷이 무엇이든 — 찾아지는 것**, 그리고 그 위의 관계 레이어입니다. 현재 인덱싱:

- **마크다운** — 핵심: frontmatter(`title`, `tags`), 헤딩, 위키링크, md 링크.
- **플레인 텍스트 산문** (`.txt`, `.rst`, `.org`, `.adoc`) — 문단 블록 단위 섹션화.
- **HTML** (`.html`, `.htm`) — 태그 스트립, `<title>`/`<h1>`을 제목으로, 헤딩 태그 단위 섹션화; 로컬 문서로 향하는 `<a href>` 앵커는 링크 그래프에 편입, `<script>`/`<style>` 제외.
- **PDF** — 표준 라이브러리만으로 텍스트 추출, 의존성 0. 까다로운 케이스(CJK·서브셋 임베딩 폰트)도 처리하고, 각 페이지를 하나의 검색 섹션으로 다룹니다. 스캔 이미지 PDF는 OCR 없이는 누구도 못 읽으므로, wikimap은 이름 기준 인덱싱으로 폴백하고 **update 출력에 그 사실을 명시합니다** — 된 척하지 않습니다.
- **이미지** (`.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`) — 내용 분석 없음; 파일명과 그 이미지를 참조하는 모든 **alt 텍스트**(`![alt](img.png)`, `<img alt=…>`)로 인덱싱하고, 이미지 참조는 링크 그래프에 편입됩니다. "그 결제 플로우 다이어그램 어디 있지?"가 이름 또는 alt로 풀립니다. `.svg`는 추가로 `<title>`/`<desc>`/텍스트 노드를 기여합니다.

코드 AST는 파싱하지 않습니다 — 코드베이스의 호출 그래프가 필요하면 코드 전용 도구를 쓰세요. wikimap은 구조를 가진 산문 코퍼스에서 빛납니다: 스펙, 정책, 계획, 노트, 리서치.

## 안정성 보장

**1.0은 인터페이스를 고정하겠다는 약속입니다.** 1.x 안에서 다음은 깨지지 않습니다:

- **CLI** — 명령어 이름, 플래그, 그 의미.
- **`--json` 출력** — 기존 필드의 이름과 타입. 단, 필드가 *추가*될 수는 있으니 모르는 키는 무시하도록 파싱하세요.
- **`.wikimap/semantics.jsonl`** — 커밋하는 파일입니다. 새 버전이 쓴 레코드 타입을 구버전이 몰라도, 구버전은 그 줄을 **건너뛰되 파일에 그대로 보존**합니다. 그래서 버전을 오르내려도 데이터가 사라지지 않습니다.

반대로 **고정하지 않는 것** — 도구가 계속 나아질 수 있도록:

- **`.wikimap/index.db`** — 스키마, 테이블, 랭킹 내부 구현. 어차피 지워도 되는 캐시입니다(`update`가 재생성). 직접 읽지 말고 CLI를 쓰세요.
- **결과 순서** — 검색 랭킹은 릴리스마다 좋아집니다. CI는 골든셋으로 *정확도*를 지키지, 순서를 고정하지 않습니다.

첫 번째 묶음을 깨야 한다면 그때가 2.0입니다. [변경 이력](CHANGELOG.md)을 참고하세요.

## 라이선스

MIT
