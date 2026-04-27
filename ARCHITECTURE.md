# 설거지수.AI — Architecture

## 서비스 구조

```
브라우저
  │
  ├─ www.sgjisu.xyz (Railway Static Service)
  │    ├─ index.html        # 단일 SPA
  │    ├─ manifest.json     # PWA
  │    ├─ service-worker.js # PWA 캐시
  │    ├─ robots.txt
  │    ├─ sitemap.xml
  │    └─ .well-known/assetlinks.json  # TWA Digital Asset Links
  │
  └─ sgjisu-production.up.railway.app (Railway FastAPI)
       ├─ server.py          # 단일 파일 백엔드
       └─ static/
            ├─ og.png
            └─ icons/        # PWA 아이콘

android/                     # TWA Android 프로젝트 (Play Store 심사 대기)
```

## 백엔드 (server.py)

### 주요 엔드포인트
| 엔드포인트 | 설명 | 캐시 |
|-----------|------|------|
| `GET /analyze` | 설거지 지수 분석 | 1시간 |
| `GET /top-movers` | 급등/급락 TOP 10 + 설거지 점수 | 5분 |
| `GET /popular` | 네이버 인기검색 TOP 5 | 1시간 |
| `GET /investor` | 외국인/기관 순매매 | - |
| `POST /log` | 방문자 로그 → Google Sheets | - |
| `GET /search` | 종목 자동완성 | - |

### 분석 파이프라인
```
/analyze 호출
  → CORP_LIST에서 종목코드 매칭
  → 상장폐지 체크 (_dead_codes)
  → 캐시 확인 (get_cached)
  → DART OpenAPI: 공시 조회 (CB/BW, 불성실공시, 번복/정정)
  → pykrx: 주가/재무 데이터
  → calc_financial(): 재무 점수 산출
  → 총점 합산 → 판정 (안전/주의/경고/위험)
  → set_cached() → 응답
```

### 설거지 지수 점수 구성 (0~100+점)
- CB/BW 발행: 최대 25점
- 불성실공시: 최대 15점
- 대주주 매도: 최대 20점
- 재무 (부채비율, 영업손실 등): 최대 40점
- 바이오 업종: 재무 가산점 대폭 경감
- 금융 업종: 부채비율·유동비율 제외

### 인메모리 캐시
```python
_cache = {}                  # corp_code → 분석 결과 (TTL 1시간)
_top_movers_cache = {}       # 급등/급락 (TTL 5분)
_popular_cache = {}          # 인기검색 (TTL 1시간)
_movers_score_cache = {}     # movers 설거지 점수 (영구, 재시작 시 초기화)
_dead_codes = set()          # 상장폐지 종목
```

### 백그라운드 작업
- 서버 시작 시: `load_corp_list()` → `filter_dead_codes()` → `_startup_score_movers()`
- movers 갱신 시: 캐시 없는 종목 `_bg_score_movers()` 자동 트리거 (1.5초 간격)

### 로깅 (Google Sheets)
```
type 값: 사용 | 체류 | 관심종목
컬럼: timestamp / type / company / stock_code / score / ip / device / region / referrer / session_time
```

## 프론트엔드 (index.html)

### 화면 구조
```
#search-screen   검색 메인
  ├─ 로고/태그라인
  ├─ 검색 인풋 + 자동완성
  ├─ 인기검색 chips (#popular-chips)
  └─ 내 종목 chips (#my-stocks-section) ← localStorage

#loading-screen  분석 중

#result-screen   분석 결과
  ├─ 슬림 탑바 (← 다시 검색 | 종목명 | ☆ 즐겨찾기 | 가격)
  ├─ 게이지 + 한줄 요약
  ├─ 탭 (공시 | 재무 | 스마트머니 | 뉴스)
  └─ CTA + 카카오 공유
```

### 색상 팔레트
| 역할 | 색상 |
|------|------|
| 페이지 배경 | #0d1117 |
| 카드 배경 | #131c2e |
| 드롭다운 | #162032 |
| 점수 초록 | #00e676 |
| 점수 빨강 | #ff3131 |
| 스마트머니 순매수 | #4488ff |
| 스마트머니 순매도 | #ff3131 |

### 점수 색상 기준
- 0~19점: 초록 (안전)
- 20~49점: 노랑 (주의)
- 50~69점: 주황 (경고)
- 70+점: 빨강 (위험)
- Movers 아이콘: 70+점 ⚠️ / 100+점 💀

### 즐겨찾기
- 저장소: `localStorage['watchlist']` (JSON 배열, 최대 20개)
- 저장 시 `/log` 호출 → Google Sheets에 `관심종목` 타입 기록

## 광고
- Kakao AdFit: `ba.min.js` head 로드, JS로 동적 `ins` 삽입
- Google AdSense: `ca-pub-2791704154946680` (심사 중)
- 단위: PC 728×90 / 모바일 320×50

## 외부 의존성
| 서비스 | 용도 |
|--------|------|
| DART OpenAPI | 공시 데이터 |
| pykrx | 주가/재무 |
| 네이버 금융 스크래핑 | movers, 인기검색, 외국인/기관 |
| ip-api.com | IP 지역 조회 |
| Google Sheets (Apps Script) | 방문자 로그 |
| Kakao AdFit | 광고 |
| Google AdSense | 광고 (심사 중) |
| Google Fonts | Bebas Neue, Noto Sans KR |
| Chart.js | 게이지 차트 |

## 인프라
- 호스팅: Railway (GitHub 자동 배포)
- 도메인: `www.sgjisu.xyz` (static) / `sgjisu-production.up.railway.app` (API)
- DB: 없음 (인메모리 캐시만 사용)
- 환경변수: `DART_API_KEY`, `SHEETS_URL`, `KRX_ID`(미사용)
