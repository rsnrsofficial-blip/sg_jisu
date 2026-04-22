# 설거지수.AI (sgjisu.xyz) — 작업 기록

## 서비스 개요
- **URL**: https://www.sgjisu.xyz/
- **호스팅**: Railway
- **스택**: FastAPI (server.py) + 단일 HTML SPA (index.html)
- **타겟**: 55~64세 시니어 개미 투자자

---

## 주요 기능
- DART OpenAPI 기반 공시 분석 (CB/BW 발행, 불성실공시, 번복/정정)
- 설거지 지수 점수 산출 (0~100점, 4단계 판정)
- 네이버 금융 스크래핑: 외국인/기관 순매매 동향 (/investor 엔드포인트)
- 네이버 금융 급등/급락 TOP 10 (movers)
- Kakao AdFit 광고 (동적 삽입 방식)
- 카카오톡 공유 버튼 (navigator.share → clipboard fallback)
- 네이버 금융 인기검색 TOP 5 (/popular 엔드포인트, 1시간 캐시)

---

## 작업 이력

### 백엔드 (server.py)

#### 상장폐지 종목 필터링
- `_dead_codes` set으로 캐싱
- `/analyze` 사전 체크: 가격 데이터 없으면 dead_codes에 추가 후 에러 반환

#### 불성실공시 감지 수정
- 기존: 잘못된 `pblntf_ty=="F"` 코드 사용
- 수정: `"불성실공시법인" in report_nm` 키워드 검색
- `page_count=100` 추가
- 거래소공시(`pblntf_ty="I"`) 별도 조회 후 합산

#### 코스피/코스닥 배지
- `stock_mket` 필드 비어있는 경우 → `corp_cls` 코드로 fallback (Y=KOSPI, K=KOSDAQ)

#### /investor 엔드포인트
- 네이버 frgn.naver 페이지 스크래핑
- 외국인/기관 5일 순매매 데이터 반환
- 실제 연속 매수/매도 streak 계산 (filter 버그 수정 → 최근일부터 연속 카운트)

#### /log 엔드포인트
- OS 감지: iOS/Android/Windows/macOS
- `window._usageLogged` 플래그로 분석 이벤트 즉시 기록 (session_time: 0)
- `type:'exit'` → `type:'체류'`로 시트에 저장 (서버에서 변환)
- goBack / beforeunload: `_usageLogged` 체크 제거 → 항상 실제 체류시간 전송

#### /popular 엔드포인트
- 네이버 금융 인기검색어 페이지 스크래핑 (EUC-KR 인코딩)
- 상위 5개 종목코드·종목명 반환
- `_popular_cache` 1시간 캐시

#### 금융업 부채비율 예외 처리
- `get_stock_market()` → `(market, is_financial, is_bio)` 튜플 반환
- `induty_code` 기준: 64/65/66 = 금융업, 27/21/72/86 = 바이오
- `calc_financial(client, corp_code, is_financial, is_bio)` 파라미터 추가
- 금융업: 부채비율·유동비율 가산점 제외, 표시만 "(금융업 기준 제외)"
- 바이오업: 영업손실/적자/순손실 가산점 대폭 경감 (업종 특성 반영)

| 항목 | 일반 | 바이오 경감 |
|------|------|------------|
| 영업손실 | +18점 | +6점 |
| 흑자→적자 전환 | +20점 | +6점 |
| 3년 중 2번+ 적자 | +12점 | +4점 |
| 순손실 10%+ | +12점 | +4점 |
| 순손실 5%+ | +6점 | +2점 |

---

### 프론트엔드 (index.html)

#### 색상 팔레트 — 네이비 다크 전환
| 역할 | 색상 |
|------|------|
| 페이지 배경 | #0d1117 |
| 카드/인풋 배경 | #162032 |
| 드롭다운/패널 | #131c2e |
| 진행바/레이어 | #1a2840 |
| 보조 패널 | #111827 |

#### 검색 페이지 레이아웃
- 헤드라인: "내 종목, 세력의 설거지 물량일까? / DART 공시 기반 팩트 체크"
- 플레이스홀더: "종목명이나 종목코드를 입력하세요"
- 상단 광고 완전 제거 (하단만 유지)
- hero-block (캐릭터 이미지 + 29층 문구) 제거
- 검색창 상하 여백 대폭 확대
- 칩 버튼화: 배경 #1e1e1e, 테두리 rgba(255,255,255,0.15), border-radius 6px
- 분석 프로세스 ①~④ 섹션 추가 (chips 아래)
- TOP 10 리스트 위 브릿지 문구: "이 종목들의 설거지 위험도도 확인해 보세요 →"
- 인기 검색 TOP 5 chips: /popular API 호출, `order:4/5`로 로고 아래 배치

#### 결과 페이지 구조
- **슬림 탑바**: 종목코드·종목명·등락가를 상단 고정 바에 한 줄 압축 (← 다시 검색 포함)
- **게이지 확대**: 240×145 → 290×175, 숫자 60px → 76px
- **플랫 카드**: 테두리 제거, border-radius 6px, 배경 #131c2e 통일
- **점수 색상 로직**:
  - 카드 서브점수용 `cardColor(v)`: 0pt=초록, 1~19=노랑, 20~29=오렌지, 30+=빨강
  - 총점용 `scoreColor(n)`: 0~19=초록, 20~49=노랑, 50~69=오렌지, 70+=빨강
  - HTML 하드코딩 색상 제거 → JS에서 점수값 기반 동적 적용
- **스마트머니 동향**: 외국인/기관 5일 바 + 신호 텍스트, 색상 방향 일관성 수정
  - 순매수=파랑(#4488ff), 순매도=빨강(#ff3131)
  - 바 색상·수치·신호 텍스트 모두 동일 기준으로 통일
- **카카오톡 공유 버튼**: 결과 페이지 CTA 위 노란 버튼, navigator.share → clipboard fallback
- **점수 범례**: 게이지 아래 0~19/20~49/50~69/70+ 색상 기준 표시
- **한줄 요약** (`#r-summary`): 주요 원인 bullet + 점수 색상 강조
- **CTA 가이던스** (`#cta-guidance`): 점수대별 행동 지침 텍스트
- **푸터**: `© 2026 설거지수.AI · All rights reserved` (사업자 정보 제거)

#### 광고 구조
- Kakao AdFit `ba.min.js` head에 1회만 로드
- 빈 div에 JS로 동적 `ins` 태그 삽입 (`_injectAd()`)
- 동일 unit ID 중복 방지
- 빈 광고 박스 플레이스홀더 제거 (투명 처리)

#### 폰트 시니어 스케일업 (1.2배)
- 카드 제목/설명, 공시 플래그 제목, 재무 항목, 탭, 타임라인 등 전반적 업스케일
- 검색 인풋 17px, 버튼 16px

#### 7-pass 디자인 리뷰 반영 (2026-04-19)
- **disclaimer-bar 이동**: 검색/결과 화면 상단 → footer-legal 바로 위로 이동
- **①②③④ 모바일 세로 배치**: `@media (max-width: 480px)` → `flex-direction: column`, 각 단계 가로 레이아웃 (번호+텍스트)
- **`#r-summary` 위치 변경**: 게이지 아래 → 게이지 위로 이동, 13px/색상 #94a3b8로 개선
- **TODAY'S MOVERS 라벨 제거**: `.movers-title` div 삭제, 날짜만 표시
- **법적 고지 텍스트 대비 상향**: `#334155` → `#64748b`, bold는 `#94a3b8`, dart-credit `#475569`
- **에러 타입 분리**:
  - server.py: `not_found` / `delisted` `error_type` 필드 추가
  - frontend: timeout → "⏱ 분석 시간 초과", delisted → "📋 상장폐지", not_found → "❌ 찾을 수 없음"

---

### 전략 (2026-04-19 /office-hours + /plan-ceo-review)

#### 현재 상황
- 누적 매출 500원, 일 방문자 1~2명
- 유료 광고 CAC > LTV → 중단
- 종토방 게릴라 마케팅 10회: "이 서비스는 뭐임?" 반응 확인

#### 채택된 전략 (A → B 순서)
**A. 커뮤니티 배포 (즉시)**
- 당일 급락 종목 종토방 + 네이버 대형 주식 카페에 "오늘 XX 급락해서 설거지수 AI로 확인해봤습니다" 형식 게시
- 코드 없음, 매일 30분

**B. 위험종목 랭킹 페이지 (2~3주 내 개발)**
- 급등/급락 TOP 20종목의 설거지 지수를 일괄 계산 → `/ranking` 페이지 공개
- Railway Cron Job 매일 오전 9시 → `/analyze` 순차 호출 (1초 딜레이, DART 레이트 리밋 방지)
- JSON 파일 캐시 (메모리 아님 — Railway 재시작 시 소실 방지)
- SEO 타겟: "코스닥 위험 종목", "오늘 급락 주식 위험도"
- 빈 캐시 폴백: "오늘 데이터 준비 중 (매일 오전 9시 업데이트)"

**C. 포트폴리오 알림 구독 (일 방문자 20명 달성 후)**
- JWT 로그인 + 종목 등록 + 카카오/이메일 알림 + 월 3,000~5,000원 구독

#### 성공 지표
- 2주: 일 방문자 10명
- 4주: 일 방문자 20명 + 카카오톡 자연 공유 1회
- 8주: 구독 의향 유저 3명과 대화

#### DEFERRED
- 카카오톡 채널: 20명/일 달성 후 재검토
- 공유 이미지 카드 자동 생성: 트래픽 생긴 후
- 네이버 블로그 콘텐츠: 패스

---

### 트래픽 & 수요 검증 (2026-04-21~22)

#### 종토방 바이럴 결과
- 2026-04-21 12:00 종토방 5개 댓글 배포 → 당일 ~35-40명 유입 (기준선 1~2명 대비 20배+)
- 포트폴리오 체커 패턴 뚜렷: 한 유저가 5~10개 종목 연속 검색 (LS 계열, 바이오 포트폴리오 등)
- 최장 체류: 메지온 1790초(30분), 에프엔씨엔터 928초, 포스코퓨처엠 1452초 — 진지하게 읽은 유저 다수
- 에프엔씨엔터 종토방 댓글 반응: "저거 어플명이 뭐임?ㅋ", "이거 뭔기요? 좋은데요" → 자발적 관심 확인

#### 수요 검증 단계 판단
- **수요 있음** (확인): 종토방 유입, 다종목 검색, 긴 체류시간
- **효용 미검증**: 아직 재방문, 자발적 공유, "써보니 맞더라" 피드백 없음
- **다음 증거**: 재방문 유저 or 자발적 카카오톡 공유 1건

---

### OG 이미지 & 공유 개선 (2026-04-21)

#### 카카오톡 링크 미리보기 썸네일
- `static/og.png` 생성 (1200×630, Pillow로 로컬 생성)
- server.py: `StaticFiles` 마운트 (`/static`)
- `og:image` 메타태그 추가 → `https://sgjisu-production.up.railway.app/static/og.png`
- `og:image:width/height`, `og:type` 추가

#### navigator.share URL 중복 수정
- 기존: `text` 안에 URL + `url` 파라미터에도 URL → KakaoTalk에서 링크 카드 2개 표시
- 수정: `text`에서 URL 제거, `url` 파라미터로만 전달 (카드 1개)
- 클립보드 fallback은 URL 포함 유지

#### Kakao Share SDK 시도 → 롤백
- 시도: `Kakao.Share.sendDefault()`로 커스텀 카드 공유
- 실패 원인: sharer.kakao.com 도메인 인증 에러 (원인 불명확)
- 결론: navigator.share 원복, URL 미리보기 썸네일 개선으로 충분

---

### SEO / 네이버 색인 (2026-04-22)

#### robots.txt + sitemap.xml 추가
- 문제: 네이버 서치어드바이저 "수집제한 1" → robots.txt 404
- 원인: `www.sgjisu.xyz`는 static 서비스, FastAPI 엔드포인트와 별개
- 해결: `robots.txt`, `sitemap.xml` 루트에 정적 파일로 추가 (index.html 옆)
- 네이버 서치어드바이저 → 요청 → 사이트맵 제출: `https://www.sgjisu.xyz/sitemap.xml`

#### SEO 현황 판단
- 지금 단계에서 SEO 우선순위 낮음: 유입 전부 종토방 직접 링크, 검색 유입 아님
- SEO가 의미 있어지는 시점: `/ranking` 페이지 완성 후 (키워드 타겟팅 가능)

---

## 주의사항
- Kakao AdFit: 같은 unit ID는 DOM에 1개만 존재해야 함
- 로그 구조: `type:'usage'`(session_time:0) = 분석 완료 즉시 / `type:'체류'`(session_time:실제초) = 이탈 시
- `goBack()` / `beforeunload`: `_usageLogged` 체크 없이 항상 체류시간 전송
- 상장폐지 체크는 autocomplete 필터링과 analyze 양쪽에서 이중으로 처리
- 불성실공시: `pblntf_ty="I"` (거래소공시) 별도 조회 필수, 일반 공시 조회만으로는 누락됨
- 외국인/기관 streak: `filter()` 개수가 아니라 최근일부터 끊기지 않은 연속일 카운트
- `get_stock_market()` 반환값 튜플 3개: `(market, is_financial, is_bio)` — 2개로 언패킹하면 에러
- 바이오 예외: 자본잠식·현금흐름·부채비율은 경감 없음 (진짜 위험 신호이므로)
- `/ranking-build`는 내부 함수 직접 호출로 구현 (HTTP 엔드포인트 노출 시 DART API 남용 가능)
- Railway Cron: `/ranking` 빌드에 사용. 타임아웃 초과 시 `asyncio.create_task` 백그라운드 처리
- 에러 타입 필드: server.py 반환값에 `error_type` 항상 포함 (`not_found` / `delisted` / 기타)
- `og:image` URL은 `sgjisu-production.up.railway.app/static/og.png` (FastAPI StaticFiles 서빙)
- `robots.txt` / `sitemap.xml`은 루트 정적 파일 (index.html 옆), FastAPI 엔드포인트 아님
- `www.sgjisu.xyz`(static 서비스)와 `sgjisu-production.up.railway.app`(FastAPI)는 별개 Railway 서비스
