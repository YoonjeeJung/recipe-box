# Link Collector

아이폰 공유 시트로 링크를 보내면 AI가 자동으로 스크래핑·요약·분류해서 노션 DB에 저장해주는 개인 지식 베이스 서비스.

## 동작 흐름

```
아이폰 공유 시트
      ↓
iOS 단축어 → HTTP POST /save
      ↓
FastAPI 서버 (Railway)
      ↓
URL 타입 감지 → 스크래퍼
      ↓
Claude API — 제목 / 요약 / 태그 / 카테고리 생성
      ↓
Notion API → DB 저장
```

## 기능

- **iOS 공유 시트 연동** — 단축어로 서버에 URL 전송
- **자동 스크래핑** — 일반 웹 / YouTube / Instagram(공개) 지원
- **AI 분석** — Claude가 제목, 3줄 요약, 카테고리, 태그, 장소 자동 추출
- **노션 저장** — 분석 결과를 노션 DB에 즉시 저장, 노션이 UI 겸 저장소

## 노션 DB 스키마

| 필드 | 타입 | 설명 |
|------|------|------|
| Title | title | AI 추출 제목 |
| Link | url | 원본 URL |
| Source | select | Instagram / YouTube / Web / Twitter / TikTok |
| Type | select | Recipe / Restaurant / Cafe / Product / Article / Video / Other |
| Tags | multi_select | Korean, Japanese, Coffee, Vegan, Dessert 등 |
| Summary | rich_text | AI 3줄 요약 |
| Status | select | 저장됨 / 가보고싶다 / 가봤다 / 만들어봤다 / 별로였다 |
| Rating | select | ⭐ ~ ⭐⭐⭐⭐⭐ |
| Location | rich_text | 장소명 / 주소 |
| Created | created_time | 자동 생성 |

## 빠른 시작

### 1. 의존성 설치

```bash
pip install -r requirements.txt
```

### 2. 환경 변수 설정

```bash
cp .env.example .env
# .env 파일을 열어 값 입력
```

```env
NOTION_TOKEN=ntn_xxxxxxxxxxxxxxxxxxxxxx
NOTION_DB_ID=10952a5c6d374bb6b44c9a9c68e6dd3c
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxxxxxxxxxxx
```

### 3. 서버 실행

```bash
uvicorn main:app --reload
```

### 4. 테스트

```bash
curl -X POST http://localhost:8000/save \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/watch?v=xxxxx"}'
```

## API

### `POST /save`

링크를 받아 스크래핑 → AI 분석 → 노션 저장

**Request**
```json
{ "url": "https://www.instagram.com/p/xxxxx/" }
```

**Response**
```json
{
  "status": "ok",
  "notion_page_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "title": "을지로 카페 추천",
  "summary": "...",
  "tags": ["Coffee", "Korean"],
  "type": "Cafe",
  "source": "Instagram"
}
```

### `GET /health`

서버 상태 확인

## Railway 배포

1. Railway에서 새 프로젝트 생성 후 이 레포 연결
2. Variables 탭에서 `.env` 값 입력
3. `Procfile`이 자동으로 서버 실행

## iOS 단축어 설정

1. 단축어 앱에서 새 단축어 생성
2. **URL 가져오기** 액션 추가
3. **URL 내용 가져오기** 액션 추가:
   - 방법: POST
   - 헤더: `Content-Type: application/json`
   - 본문: JSON → `{"url": "단축어 입력"}`
   - URL: `https://your-app.railway.app/save`
4. 공유 시트에서 실행 가능하도록 설정

## 레포지토리 구조

```
link-collector/
├── main.py          # FastAPI 앱 진입점
├── scraper.py       # URL 타입별 스크래퍼
├── ai.py            # Claude API 연동
├── notion.py        # 노션 API 연동
├── requirements.txt
├── .env.example
├── .gitignore
└── Procfile
```

## URL별 스크래핑 지원 현황

| 소스 | 방법 | 상태 |
|------|------|------|
| 일반 웹 | BeautifulSoup (og 태그 + 본문) | ✅ 안정 |
| YouTube | oEmbed API | ✅ 안정 |
| Instagram | 공개 oEmbed | ⚠️ 불안정 |
| TikTok | oEmbed API | 🟡 제한적 |

> ⚠️ Instagram 스크래핑은 공개 계정만 가능하며 이용약관 위반 위험이 있습니다.
