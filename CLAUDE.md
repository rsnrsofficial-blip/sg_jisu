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
