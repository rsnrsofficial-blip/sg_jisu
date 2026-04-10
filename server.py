import zipfile, io, xml.etree.ElementTree as ET, re, os, time, threading
import json, asyncio
import pandas as pd
import httpx
import requests as sync_requests
from fastapi.responses import JSONResponse
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timedelta
from pykrx import stock as krx


# ── UTF-8 JSON 응답 ──
class UTF8JSONResponse(JSONResponse):
    def render(self, content) -> bytes:
        return json.dumps(content, ensure_ascii=False).encode('utf-8')


app = FastAPI(default_response_class=UTF8JSONResponse)


@app.middleware("http")
async def add_utf8_charset(request: Request, call_next):
    response = await call_next(request)
    ct = response.headers.get("content-type", "")
    if "application/json" in ct and "charset" not in ct:
        response.headers["content-type"] = "application/json; charset=utf-8"
    return response


app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

API_KEY = os.getenv("DART_API_KEY")
if not API_KEY:
    raise ValueError("DART_API_KEY 환경변수가 설정되지 않았어요!")

SHEETS_URL = "https://script.google.com/macros/s/AKfycbzJE6odne2kSuIdKqVY9xP1KWgSZUx67AyJeK_WQh2EVuasNgZ89ye22tJqVeVFBSHp/exec"


# ── 구글 시트 로그 전송 (비동기) ──
def log_to_sheets(data: dict):
    def _send():
        try:
            sync_requests.post(SHEETS_URL, json=data, timeout=5)
            print(f"   📊 시트 기록: {data.get('company')} / {data.get('ip')}")
        except Exception as e:
            print(f"   ⚠️ 시트 기록 실패: {e}")
    threading.Thread(target=_send, daemon=True).start()


# ── 메모리 캐시 ──
_cache = {}
CACHE_TTL = 3600
_analyzed_cache_list = []  # 최근 분석 종목 (업종 경고용)


def get_cached(corp_code):
    if corp_code in _cache:
        ts, data = _cache[corp_code]
        if time.time() - ts < CACHE_TTL:
            print(f"   ✅ 캐시 반환: {corp_code}")
            return data
    return None


def set_cached(corp_code, data):
    _cache[corp_code] = (time.time(), data)


# ── 공시 본문 캐시 ──
_doc_cache = {}

CORP_LIST = []
CORP_LIST_READY = False
_dead_codes: set = set()  # 상폐/거래정지 등 비활성 종목 코드


def load_corp_list():
    global CORP_LIST, CORP_LIST_READY
    try:
        print("📥 회사 목록 다운로드 중...")
        res = sync_requests.get("https://opendart.fss.or.kr/api/corpCode.xml",
                                params={"crtfc_key": API_KEY}, timeout=30)
        z = zipfile.ZipFile(io.BytesIO(res.content))
        root = ET.fromstring(z.read("CORPCODE.xml"))
        for c in root.findall("list"):
            sc = c.findtext("stock_code", "").strip()
            if sc:
                CORP_LIST.append({
                    "corp_code": c.findtext("corp_code", ""),
                    "corp_name": c.findtext("corp_name", ""),
                    "stock_code": sc,
                })
        CORP_LIST_READY = True
        print(f"✅ 총 {len(CORP_LIST)}개 상장사 로드 완료")
        filter_dead_codes()
    except Exception as e:
        print(f"❌ 회사 목록 로드 실패: {e}")


def filter_dead_codes():
    """동명 중복 종목 중 거래 데이터 없는 상폐 종목을 _dead_codes에 추가 (load_corp_list 완료 직후 호출)"""
    global _dead_codes
    from collections import Counter
    name_cnt = Counter(c["corp_name"] for c in CORP_LIST)
    dup_names = {n for n, cnt in name_cnt.items() if cnt > 1}
    print(f"   🔍 중복 회사명 {len(dup_names)}개 검사 시작...")
    checked = 0
    for c in CORP_LIST:
        if c["corp_name"] not in dup_names:
            continue
        try:
            df = get_price_data(c["stock_code"])
            if df is None or len(df) == 0 or df["거래량"].sum() == 0:
                _dead_codes.add(c["stock_code"])
                print(f"   🚫 상폐 감지: {c['corp_name']} ({c['stock_code']})")
            checked += 1
        except Exception:
            pass
    print(f"✅ 중복명 {checked}개 검사 완료, 상폐 {len(_dead_codes)}개 필터")


threading.Thread(target=load_corp_list, daemon=True).start()


def _is_active(stock_code: str) -> bool:
    return stock_code not in _dead_codes


def search_corp(name):
    # 종목코드 직접 입력 처리
    if re.fullmatch(r'\d{6}', name):
        for c in CORP_LIST:
            if c["stock_code"] == name:
                return c["corp_code"], c["corp_name"], c["stock_code"]
        return None, None, None

    # 활성 종목 중 정확히 일치하는 것 먼저
    exact_active = [c for c in CORP_LIST if c["corp_name"] == name and _is_active(c["stock_code"])]
    if exact_active:
        return exact_active[0]["corp_code"], exact_active[0]["corp_name"], exact_active[0]["stock_code"]

    # 활성 종목 중 이름 포함 검색
    for c in CORP_LIST:
        if name in c["corp_name"] and _is_active(c["stock_code"]):
            return c["corp_code"], c["corp_name"], c["stock_code"]

    # fallback: 활성 필터 없이 재시도
    for c in CORP_LIST:
        if c["corp_name"] == name:
            return c["corp_code"], c["corp_name"], c["stock_code"]
    for c in CORP_LIST:
        if name in c["corp_name"]:
            return c["corp_code"], c["corp_name"], c["stock_code"]
    return None, None, None


# ──────────────────────────────────────────
# 시장 구분 (KOSPI/KOSDAQ) 조회
# ──────────────────────────────────────────
async def get_stock_market(client, corp_code):
    data = await dart_get(client, "https://opendart.fss.or.kr/api/company.json", {
        "crtfc_key": API_KEY, "corp_code": corp_code
    })
    mkt = data.get("stock_mket", "")
    if "유가" in mkt:
        market = "KOSPI"
    elif "코스닥" in mkt:
        market = "KOSDAQ"
    elif "코넥스" in mkt:
        market = "KONEX"
    else:
        cls = data.get("corp_cls", "")
        if cls == "Y":
            market = "KOSPI"
        elif cls == "K":
            market = "KOSDAQ"
        elif cls == "N":
            market = "KONEX"
        else:
            market = "—"
    # 업종코드: 금융업 여부 판단 (K코드: 64~66 금융/보험, 증권)
    induty_code = data.get("induty_code", "")
    is_financial = induty_code.startswith(("64", "65", "66")) if induty_code else False
    return market, is_financial


# ──────────────────────────────────────────
# 비동기 DART 요청 헬퍼
# ──────────────────────────────────────────
async def dart_get(client: httpx.AsyncClient, url: str, params: dict) -> dict:
    try:
        res = await client.get(url, params=params, timeout=10)
        return res.json()
    except Exception:
        return {}


async def get_fin_items_async(client, corp_code, year, reprt_code="11011", fs_div="CFS"):
    data = await dart_get(client, "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json", {
        "crtfc_key": API_KEY, "corp_code": corp_code,
        "bsns_year": str(year), "reprt_code": reprt_code, "fs_div": fs_div,
    })
    if data.get("status") != "000":
        return None
    return data.get("list", [])


async def get_공시목록_async(client, corp_code, days, pblntf_ty=None):
    params = {
        "crtfc_key": API_KEY, "corp_code": corp_code,
        "bgn_de": (datetime.now() - timedelta(days=days)).strftime("%Y%m%d"),
        "end_de": datetime.now().strftime("%Y%m%d"),
        "page_count": 100,
    }
    if pblntf_ty:
        params["pblntf_ty"] = pblntf_ty
    data = await dart_get(client, "https://opendart.fss.or.kr/api/list.json", params)
    return data.get("list", [])


async def get_document_async(client, rcept_no):
    if rcept_no in _doc_cache:
        return _doc_cache[rcept_no]
    try:
        res = await client.get("https://opendart.fss.or.kr/api/document.json",
                               params={"crtfc_key": API_KEY, "rcept_no": rcept_no}, timeout=5)
        text = res.text
        _doc_cache[rcept_no] = text
        return text
    except:
        return ""


def parse_val(items, *keywords):
    def safe_int(v):
        try: return int(str(v).replace(",", "")) if v else 0
        except: return 0
    for kw in keywords:
        for item in items:
            if kw == item.get("account_nm", "").strip():
                return safe_int(item.get("thstrm_amount")), safe_int(item.get("frmtrm_amount")), safe_int(item.get("bfefrmtrm_amount"))
        for item in items:
            nm = item.get("account_nm", "")
            if kw in nm and "누계" not in nm and "분기" not in nm:
                return safe_int(item.get("thstrm_amount")), safe_int(item.get("frmtrm_amount")), safe_int(item.get("bfefrmtrm_amount"))
    return 0, 0, 0


# ──────────────────────────────────────────
# KRX 주가 조회 (급락 감지용)
# ──────────────────────────────────────────
def get_price_data(stock_code):
    try:
        오늘 = datetime.now().strftime("%Y%m%d")
        이주전 = (datetime.now() - timedelta(days=14)).strftime("%Y%m%d")
        df = krx.get_market_ohlcv_by_date(이주전, 오늘, stock_code)
        if df is None or len(df) < 2:
            return None
        return df
    except:
        return None


def check_krx_status(stock_code):
    결과 = {"거래정지": False, "관리종목": False, "메시지": [], "급락": False, "급락률": 0}
    try:
        df = get_price_data(stock_code)
        if df is None or len(df) == 0:
            결과["거래정지"] = True
            결과["메시지"].append("🔴 거래정지 — 최근 7일 거래 데이터 없음")
            print(f"   🔴 거래정지 감지: {stock_code}")
        else:
            최근거래량 = df["거래량"].tail(3).sum()
            if 최근거래량 == 0:
                결과["거래정지"] = True
                결과["메시지"].append("🔴 거래정지 — 최근 3일 거래량 0")
            if len(df) >= 4:
                기준가 = float(df["종가"].iloc[-4])
                현재가 = float(df["종가"].iloc[-1])
                if 기준가 > 0:
                    급락률 = (현재가 - 기준가) / 기준가 * 100
                    결과["급락률"] = round(급락률, 2)
                    if 급락률 <= -15:
                        결과["급락"] = True
                        print(f"   📉 주가 급락 감지: {급락률:.1f}%")
    except Exception as e:
        print(f"   KRX 조회 오류: {e}")
    return 결과


# ── 즉사 판정 ──
def check_instant_death(공시목록_2년, stock_code, 자본금, 자본총계):
    즉사_키워드 = [
        ("상장폐지사유", "⛔ 상장폐지 사유 발생"),
        ("감사의견거절", "🚫 감사의견 거절"),
        ("감사의견부적정", "🚫 감사의견 부적정"),
        ("파산신청", "💀 파산 신청"),
        ("회생절차개시", "🆘 회생절차 개시"),
    ]
    감지된_메시지 = []
    for 공시 in 공시목록_2년:
        제목 = 공시.get("report_nm", "").replace(" ", "")
        for kw, msg in 즉사_키워드:
            if kw in 제목 and msg not in 감지된_메시지:
                감지된_메시지.append(msg)
                print(f"   💀 즉사 키워드 감지: {kw}")
    krx_상태 = check_krx_status(stock_code)
    if krx_상태["거래정지"]:
        for msg in krx_상태["메시지"]:
            if msg not in 감지된_메시지:
                감지된_메시지.append(msg)
    if 자본금 > 0 and 자본총계 < 0:
        msg = "💀 완전자본잠식 — 자본이 마이너스!"
        if msg not in 감지된_메시지:
            감지된_메시지.append(msg)
        print(f"   💀 완전자본잠식 감지")
    if 감지된_메시지:
        return True, 감지된_메시지, krx_상태
    return False, [], krx_상태


# ──────────────────────────────────────────
# S1. 자금 오염도 (async + 병렬 본문 조회)
# ──────────────────────────────────────────
async def calc_funding(client, corp_code, 공시목록_1년):
    cb_목록 = []; 투자조합_건수 = 0; 총발행금액 = 0; 제3자_유증 = 0

    cb_공시 = [
        c for c in 공시목록_1년
        if ("전환사채" in c.get("report_nm", "") or "신주인수권" in c.get("report_nm", ""))
        and "[기재정정]" not in c.get("report_nm", "")
        and "취득" not in c.get("report_nm", "")
        and "상환" not in c.get("report_nm", "")
        and "소각" not in c.get("report_nm", "")
    ]

    docs = await asyncio.gather(*[get_document_async(client, c.get("rcept_no", "")) for c in cb_공시[:10]])

    for 공시, text in zip(cb_공시[:10], docs):
        제목 = 공시.get("report_nm", "")
        is_fund = any(k in text for k in ["투자조합", "사모", "유한책임회사", "PEF"])
        amount = 0
        m = re.findall(r'(\d[\d,]+)억\s*원', text)
        if m: amount = int(m[0].replace(",", ""))
        if is_fund: 투자조합_건수 += 1
        총발행금액 += amount
        cb_목록.append({"title": 제목, "is_fund": is_fund, "amount": amount})

    for c in 공시목록_1년:
        제목 = c.get("report_nm", "")
        if "제3자배정" in 제목.replace(" ", "") or ("유상증자" in 제목 and "제3자" in 제목):
            제3자_유증 += 1

    cb_개수 = len(cb_목록)
    점수 = 0
    if cb_개수 >= 7:   점수 += 40
    elif cb_개수 >= 5: 점수 += 35
    elif cb_개수 >= 3: 점수 += 25
    elif cb_개수 >= 1: 점수 += 10
    if 투자조합_건수 >= 1: 점수 += 10
    if 총발행금액 >= 100: 점수 += 5
    if 총발행금액 < 10 and cb_개수 > 0: 점수 -= 5
    if 제3자_유증 >= 2: 점수 += 15
    elif 제3자_유증 == 1: 점수 += 8
    if cb_개수 >= 1 and 제3자_유증 >= 1: 점수 += 10
    return max(0, min(60, 점수)), cb_개수, cb_목록, 투자조합_건수, 총발행금액, 제3자_유증


# ──────────────────────────────────────────
# S2. 신뢰도 결여 (공시 번복/정정 감지 포함)
# ──────────────────────────────────────────
async def calc_trust(client, corp_code, 공시목록_2년, 공시목록_6개월):
    # 거래소공시(I) 별도 조회 - 불성실공시법인지정은 page_count=10 기본값에 묻힐 수 있음
    거래소공시 = await get_공시목록_async(client, corp_code, 730, pblntf_ty="I")
    불성실_개수 = sum(1 for c in (공시목록_2년 + 거래소공시) if "불성실공시법인" in c.get("report_nm", ""))
    점수 = 30 if 불성실_개수 >= 2 else 20 if 불성실_개수 == 1 else 0

    번복_키워드 = ["[기재정정]", "[내용정정]", "[취소]", "[撤回]", "계약해지", "계약취소", "공급계약해지"]
    번복_목록 = []
    for c in 공시목록_6개월:
        제목 = c.get("report_nm", "")
        for kw in 번복_키워드:
            if kw in 제목 and 제목 not in 번복_목록:
                번복_목록.append(제목)
                break

    번복_개수 = len(번복_목록)
    if 번복_개수 >= 5:   점수 += 30
    elif 번복_개수 >= 3: 점수 += 20
    elif 번복_개수 >= 1: 점수 += 10

    print(f"   신뢰도: 불성실{불성실_개수}건 + 번복/정정{번복_개수}건 → {점수}점")
    return min(40, 점수), 불성실_개수, 번복_개수, 번복_목록


# ──────────────────────────────────────────
# S3. 내부자 엑시트
# ──────────────────────────────────────────
async def calc_insider(client, corp_code, stock_code, 공시목록_1개월):
    매도_목록 = []; 매도_주식수 = 0
    매도_공시 = [c for c in 공시목록_1개월 if "처분" in c.get("report_nm", "") or "매도" in c.get("report_nm", "")]
    docs = await asyncio.gather(*[get_document_async(client, c.get("rcept_no", "")) for c in 매도_공시[:5]])
    for 공시, text in zip(매도_공시[:5], docs):
        nums = re.findall(r'(\d[\d,]+)주', text)
        if nums: 매도_주식수 += int(nums[0].replace(",", ""))
        매도_목록.append(공시.get("report_nm", ""))

    발행주식수 = 0
    try:
        data = await dart_get(client, "https://opendart.fss.or.kr/api/stockTotqySttus.json", {
            "crtfc_key": API_KEY, "corp_code": corp_code,
            "bgn_de": (datetime.now() - timedelta(days=365)).strftime("%Y%m%d"),
            "end_de": datetime.now().strftime("%Y%m%d"),
        })
        items = data.get("list", [])
        if items:
            발행주식수 = int(str(items[0].get("istc_totqy", "0")).replace(",", ""))
    except: pass

    점수 = 0; 매도비율 = 0
    if 발행주식수 > 0 and 매도_주식수 > 0:
        매도비율 = 매도_주식수 / 발행주식수 * 100
        if 매도비율 >= 5:   점수 = 30
        elif 매도비율 >= 2: 점수 = 20
        elif 매도비율 >= 1: 점수 = 10
        elif 매도_목록:     점수 = 5
    elif 매도_목록: 점수 = 5
    print(f"   내부자 매도: {매도_주식수:,}주 ({매도비율:.2f}%) → {점수}점")
    return 점수, 매도_목록, 매도_주식수, 매도비율


# ──────────────────────────────────────────
# S4. 재무 위험도 (병렬 조회)
# ──────────────────────────────────────────
async def calc_financial(client, corp_code, is_financial=False):
    try:
        올해 = datetime.now().year
        작년 = 올해 - 1

        results = await asyncio.gather(
            get_fin_items_async(client, corp_code, 작년, "11011", "CFS"),
            get_fin_items_async(client, corp_code, 작년, "11011", "OFS"),
            get_fin_items_async(client, corp_code, 작년 - 1, "11011", "CFS"),
            get_fin_items_async(client, corp_code, 작년 - 1, "11011", "OFS"),
            get_fin_items_async(client, corp_code, 작년 - 2, "11011", "CFS"),
            get_fin_items_async(client, corp_code, 작년 - 2, "11011", "OFS"),
        )
        items_y0 = results[0] or results[1]
        items_y1 = results[2] or results[3]
        items_y2 = results[4] or results[5]

        분기_사용 = False
        if not items_y0:
            q = await asyncio.gather(
                get_fin_items_async(client, corp_code, 올해, "11014", "CFS"),
                get_fin_items_async(client, corp_code, 올해, "11014", "OFS"),
                get_fin_items_async(client, corp_code, 작년, "11014", "CFS"),
                get_fin_items_async(client, corp_code, 작년, "11014", "OFS"),
            )
            items_y0 = q[0] or q[1] or q[2] or q[3]
            if items_y0: 분기_사용 = True

        if not items_y0:
            return 0, {}, [], 0, 0

        def g(items, *kws):
            return parse_val(items, *kws) if items else (0, 0, 0)

        매출0, _, _     = g(items_y0, "매출액", "영업수익", "수익(매출액)")
        영업0, _, _     = g(items_y0, "영업이익", "영업손익", "영업이익(손실)")
        순이익0, _, _   = g(items_y0, "당기순이익", "당기순손익", "당기순이익(손실)")
        부채0, _, _     = g(items_y0, "부채총계")
        자본0, _, _     = g(items_y0, "자본총계")
        자본금0, _, _   = g(items_y0, "자본금")
        유동자산0, _, _ = g(items_y0, "유동자산")
        유동부채0, _, _ = g(items_y0, "유동부채")
        이자비용0, _, _ = g(items_y0, "이자비용", "금융비용")
        OCF0, _, _      = g(items_y0, "영업활동현금흐름", "영업활동으로인한현금흐름")
        매출1, _, _     = g(items_y1, "매출액", "영업수익", "수익(매출액)")
        영업1, _, _     = g(items_y1, "영업이익", "영업손익", "영업이익(손실)")
        부채1, _, _     = g(items_y1, "부채총계")
        자본1, _, _     = g(items_y1, "자본총계")
        자본금1, _, _   = g(items_y1, "자본금")
        OCF1, _, _      = g(items_y1, "영업활동현금흐름", "영업활동으로인한현금흐름")
        영업2, _, _     = g(items_y2, "영업이익", "영업손익", "영업이익(손실)")
        매출2, _, _     = g(items_y2, "매출액", "영업수익", "수익(매출액)")
        OCF2, _, _      = g(items_y2, "영업활동현금흐름", "영업활동으로인한현금흐름")

        if 분기_사용 and 매출0 > 0:
            배수 = 4 / 3
            매출0 = int(매출0 * 배수); 영업0 = int(영업0 * 배수)
            순이익0 = int(순이익0 * 배수); OCF0 = int(OCF0 * 배수) if OCF0 else 0

        print(f"   매출:{매출0} 영업:{영업0} 자본금:{자본금0} 자본총계:{자본0}")
        점수 = 0; 결과 = {}; 위험항목 = []

        if 자본금0 > 0:
            if 자본0 < 0:
                점수 += 30; 결과["⚠️ 자본잠식"] = "완전자본잠식"
                위험항목.append("완전자본잠식 — 자본이 마이너스!")
            elif 0 < 자본0 < 자본금0:
                잠식률0 = (자본금0 - 자본0) / 자본금0 * 100
                잠식률1 = (자본금1 - 자본1) / 자본금1 * 100 if 자본금1 > 0 and 0 < 자본1 < 자본금1 else 0
                잠식_가속 = 잠식률0 - 잠식률1
                결과["⚠️ 자본잠식률"] = f"{잠식률0:.0f}%"
                if 잠식률0 >= 50:
                    점수 += 25; 위험항목.append(f"자본잠식 {잠식률0:.0f}% — 관리종목 수준!")
                elif 잠식률0 > 0:
                    점수 += 12; 위험항목.append(f"자본잠식 시작 ({잠식률0:.0f}%)")
                if 잠식_가속 >= 30:
                    점수 += 15; 위험항목.append(f"자본잠식 급속 악화 (+{잠식_가속:.0f}%p)")
                elif 잠식_가속 >= 15:
                    점수 += 8; 위험항목.append(f"자본잠식 진행 중 (+{잠식_가속:.0f}%p)")

        if 매출0 > 0:
            이익률0 = 영업0 / 매출0 * 100
            결과["💰 100원 팔면 얼마 남나"] = f"{이익률0:.1f}%"
            if 영업0 < 0:
                점수 += 18; 위험항목.append(f"장사해서 손해봄 ({이익률0:.1f}%)")
            elif 이익률0 < 1:
                점수 += 15; 위험항목.append(f"100원 팔아도 1원도 못 남김 ({이익률0:.1f}%)")
            elif 이익률0 < 3:
                점수 += 8; 위험항목.append(f"수익성 매우 낮음 ({이익률0:.1f}%)")
            elif 이익률0 < 5:
                점수 += 4; 위험항목.append(f"수익성 낮음 ({이익률0:.1f}%)")
            if 매출1 > 0 and 영업1 != 0:
                이익률1 = 영업1 / 매출1 * 100
                매출증가 = (매출0 - 매출1) / abs(매출1) * 100
                이익률변화 = 이익률0 - 이익률1
                if 매출증가 > 5 and 이익률변화 < -5:
                    점수 += 12
                    위험항목.append(f"매출-이익 데드크로스 (매출+{매출증가:.1f}% / 이익률{이익률변화:+.1f}%p)")
                    결과["📉 데드크로스"] = f"매출+{매출증가:.1f}% / 이익률{이익률변화:+.1f}%p"

        영업이력 = [x for x in [영업2, 영업1, 영업0] if x != 0]
        if len(영업이력) >= 3:
            연속감소 = all(영업이력[i] > 영업이력[i+1] for i in range(len(영업이력)-1))
            감소횟수 = sum(1 for i in range(len(영업이력)-1) if 영업이력[i] > 영업이력[i+1])
            if 연속감소:
                점수 += 18; 위험항목.append("영업이익 3년 연속 감소")
            elif 감소횟수 >= 2:
                점수 += 10; 위험항목.append("영업이익 지속 감소 추세")
            if 영업이력[-2] > 0 and 영업이력[-1] < 0:
                점수 += 20; 위험항목.append("흑자→적자 전환 (위험)")
            적자횟수 = sum(1 for x in 영업이력 if x < 0)
            if 적자횟수 >= 2:
                점수 += 12; 위험항목.append(f"최근 3년 중 {적자횟수}번 적자")
            부호변화 = sum(1 for i in range(len(영업이력)-1) if (영업이력[i] > 0) != (영업이력[i+1] > 0))
            if 부호변화 >= 2:
                점수 += 10; 위험항목.append("흑자↔적자 반복 (경영 불안정)")

        OCF이력 = [x for x in [OCF2, OCF1, OCF0] if x != 0]
        if OCF이력:
            OCF적자수 = sum(1 for x in OCF이력 if x < 0)
            결과["💸 영업현금흐름"] = f"{'적자' if OCF0 < 0 else '흑자'} ({OCF0/100000000:.0f}억)"
            if OCF적자수 >= 3:
                점수 += 20; 위험항목.append("영업현금흐름 3년 연속 마이너스")
            elif OCF적자수 >= 2:
                점수 += 10; 위험항목.append("영업현금흐름 2년 연속 마이너스")
            elif OCF0 < 0:
                점수 += 5; 위험항목.append("영업현금흐름 마이너스")

        if 매출0 > 0 and 순이익0 != 0:
            순이익률 = 순이익0 / 매출0 * 100
            결과["📊 최종적으로 남은 돈"] = f"{순이익률:.1f}%"
            if 순이익률 < -10:
                점수 += 12; 위험항목.append(f"최종 손실 매출의 10% 이상 ({순이익률:.1f}%)")
            elif 순이익률 < -5:
                점수 += 6; 위험항목.append(f"최종 손실 지속 ({순이익률:.1f}%)")

        if 매출1 > 0:
            매출증가율 = (매출0 - 매출1) / abs(매출1) * 100
            결과["📈 매출 증감"] = f"{매출증가율:+.1f}%"
            if 매출증가율 < -20:
                점수 += 8; 위험항목.append(f"매출 급감 ({매출증가율:.1f}%)")
            elif 매출증가율 < -10:
                점수 += 4; 위험항목.append(f"매출 감소 중 ({매출증가율:.1f}%)")

        if 자본0 > 0:
            부채비율0 = 부채0 / 자본0 * 100
            if is_financial:
                결과["🏦 빚이 얼마나 많나"] = f"{부채비율0:.0f}% (금융업 기준 제외)"
            else:
                결과["🏦 빚이 얼마나 많나"] = f"{부채비율0:.0f}%"
                if 부채비율0 >= 400:
                    점수 += 10; 위험항목.append(f"부채비율 극위험 ({부채비율0:.0f}%)")
                elif 부채비율0 >= 200:
                    점수 += 5; 위험항목.append(f"부채비율 주의 ({부채비율0:.0f}%)")
                if 자본1 > 0 and 부채1 > 0:
                    부채비율1 = 부채1 / 자본1 * 100
                    if 부채비율0 - 부채비율1 >= 100:
                        점수 += 15
                        위험항목.append(f"부채비율 1년새 {부채비율0-부채비율1:.0f}%p 폭증!")
                        결과["💣 부채 폭증"] = f"+{부채비율0-부채비율1:.0f}%p"

        if 유동부채0 > 0 and not is_financial:
            유동비율 = 유동자산0 / 유동부채0 * 100
            결과["💳 단기 자금 여유"] = f"{유동비율:.0f}%"
            if 유동비율 < 80:
                점수 += 8; 위험항목.append(f"단기 자금 위험 ({유동비율:.0f}%)")
            elif 유동비율 < 100:
                점수 += 4; 위험항목.append(f"단기 자금 빠듯함 ({유동비율:.0f}%)")

        if 이자비용0 > 0:
            이자보상 = 영업0 / 이자비용0
            결과["🏧 이자 낼 능력"] = f"{이자보상:.1f}x"
            if 이자보상 < 0:
                점수 += 8; 위험항목.append(f"이자도 못 갚는 상태 ({이자보상:.1f}x)")
            elif 이자보상 < 1.5:
                점수 += 4; 위험항목.append(f"이자 내기 빠듯함 ({이자보상:.1f}x)")

        if 자본0 > 0 and 순이익0 != 0:
            roe = 순이익0 / 자본0 * 100
            결과["🎯 투자금 대비 수익률"] = f"{roe:.1f}%"
            if roe < -15:
                점수 += 10; 위험항목.append(f"ROE 심각 ({roe:.1f}%)")
            elif roe < -5:
                점수 += 6; 위험항목.append(f"ROE 마이너스 ({roe:.1f}%)")

        print(f"   재무점수: {점수}점")
        return min(60, 점수), 결과, 위험항목, 자본금0, 자본0
    except Exception as e:
        print(f"   재무분석 오류: {e}")
        return 0, {}, [], 0, 0


# ──────────────────────────────────────────
# S5. 최대주주 변경 + 공시 패턴
# ──────────────────────────────────────────
def calc_ownership(공시목록_3년):
    변경횟수 = 0; 횡령배임 = 0; 상호변경 = 0; 테마사업 = 0; 경영진교체 = 0
    테마_키워드 = ["AI", "인공지능", "블록체인", "NFT", "메타버스", "양자", "로봇", "우주"]
    for 공시 in 공시목록_3년:
        제목 = 공시.get("report_nm", ""); j = 제목.replace(" ", "")
        if "최대주주변경" in j or ("최대주주" in 제목 and "변경" in 제목): 변경횟수 += 1
        if "횡령" in 제목 or "배임" in 제목: 횡령배임 += 1
        if "상호변경" in j or ("상호" in 제목 and "변경" in 제목): 상호변경 += 1
        if any(kw in 제목 for kw in 테마_키워드) and "사업목적" in 제목: 테마사업 += 1
        if "대표이사변경" in j or ("대표이사" in 제목 and "변경" in 제목): 경영진교체 += 1
    점수 = 0; 위험내용 = []
    if 변경횟수 >= 3:   점수 += 25; 위험내용.append(f"최대주주 3년간 {변경횟수}회 변경 — 전형적인 작전주 패턴!")
    elif 변경횟수 == 2: 점수 += 15; 위험내용.append(f"최대주주 2회 변경 — 세력 개입 의심")
    elif 변경횟수 == 1: 점수 += 5;  위험내용.append(f"최대주주 변경 1회")
    if 횡령배임 >= 1:   점수 += 25; 위험내용.append("횡령·배임 공시 감지!")
    if 상호변경 >= 2:   점수 += 15; 위험내용.append(f"3년 내 상호 {상호변경}회 변경 — 이미지 세탁 의심")
    elif 상호변경 == 1: 점수 += 5;  위험내용.append("상호 변경 1회")
    if 테마사업 >= 1:   점수 += 10; 위험내용.append("본업 무관 테마 사업목적 추가 감지")
    if 경영진교체 >= 2: 점수 += 10; 위험내용.append(f"경영진 {경영진교체}회 교체 — 경영 불안정")
    print(f"   주주패턴: 변경{변경횟수} 횡령배임{횡령배임} → {점수}점")
    return min(40, 점수), 변경횟수, 위험내용


# ──────────────────────────────────────────
# S6. 감사/상장 위험
# ──────────────────────────────────────────
def calc_audit_risk(공시목록_2년):
    점수 = 0; 위험내용 = []; 감지된것 = set()
    위험키워드 = {
        "감사의견거절":       ("🚫 감사의견 거절", 40),
        "감사의견부적정":     ("🚫 감사의견 부적정", 35),
        "계속기업":           ("⚠️ 계속기업 불확실성 경고", 30),
        "파산신청":           ("💀 파산신청", 45),
        "회생절차":           ("🆘 회생절차 개시", 40),
        "상장폐지사유":       ("⛔ 상장폐지 사유 발생", 40),
        "상장적격성실질심사": ("⚠️ 상장적격성 심사", 30),
        "관리종목지정":       ("⚠️ 관리종목 지정", 35),
        "거래실적부진":       ("⚠️ 거래실적 부진 — 관리종목", 35),
        "자기자본50":         ("📉 자본잠식 50% 이상", 25),
    }
    for 공시 in 공시목록_2년:
        제목 = 공시.get("report_nm", "").replace(" ", "")
        for kw, (설명, 가산점) in 위험키워드.items():
            if kw in 제목 and kw not in 감지된것:
                감지된것.add(kw); 점수 += 가산점; 위험내용.append(설명)
    print(f"   감사/상장위험: {점수}점")
    return min(60, 점수), 위험내용


# ──────────────────────────────────────────
# S7. 주가 급락 + 공시 디커플링 감지
# ──────────────────────────────────────────
def calc_price_anomaly(stock_code, krx_상태, 공시목록_1개월):
    점수 = 0; 위험내용 = []
    급락률 = krx_상태.get("급락률", 0)
    if not krx_상태.get("급락", False):
        return 0, []
    악재_키워드 = ["상장폐지", "감사의견", "파산", "회생", "횡령", "배임", "손실", "적자전환"]
    최근_악재_공시 = []
    일주일전 = datetime.now() - timedelta(days=7)
    for 공시 in 공시목록_1개월:
        제목 = 공시.get("report_nm", "")
        접수일 = 공시.get("rcept_dt", "")
        try:
            if datetime.strptime(접수일, "%Y%m%d") >= 일주일전:
                if any(kw in 제목 for kw in 악재_키워드):
                    최근_악재_공시.append(제목)
        except: pass
    if not 최근_악재_공시:
        if 급락률 <= -40:
            점수 += 50; 위험내용.append(f"⚡ 공식 악재 없이 주가 {급락률:.1f}% 폭락 — 허위공시 또는 미공개 악재 강력 의심!")
        elif 급락률 <= -25:
            점수 += 35; 위험내용.append(f"⚡ 공식 악재 없이 주가 {급락률:.1f}% 폭락 — 미공개 악재 또는 허위공시 의심!")
        elif 급락률 <= -15:
            점수 += 20; 위험내용.append(f"⚡ 공식 악재 없이 주가 {급락률:.1f}% 급락 — 시장 신뢰 훼손 의심")
    else:
        if 급락률 <= -15:
            점수 += 5; 위험내용.append(f"📉 주가 {급락률:.1f}% 급락 (공시 악재 연동)")
    print(f"   주가이상: 급락률{급락률}% / 악재공시{len(최근_악재_공시)}건 → {점수}점")
    return min(60, 점수), 위험내용


# ──────────────────────────────────────────
# API 엔드포인트
# ──────────────────────────────────────────
@app.get("/")
def health():
    return {"status": "ok", "ready": CORP_LIST_READY, "corps": len(CORP_LIST)}


@app.get("/analyze")
async def analyze(name: str = "", code: str = "", request: Request = None):
    if not CORP_LIST_READY:
        return {"error": "서버 준비 중이에요. 잠시 후 다시 시도해주세요 (약 30초)"}

    # 종목코드가 주어지면 코드로 직접 매칭 (동명이인 방지)
    if code:
        matched = next((c for c in CORP_LIST if c["stock_code"] == code), None)
        if matched:
            corp_code, corp_name, stock_code = matched["corp_code"], matched["corp_name"], matched["stock_code"]
        else:
            corp_code, corp_name, stock_code = search_corp(name)
    else:
        corp_code, corp_name, stock_code = search_corp(name)

    if not corp_code:
        return {"error": f"'{name}' 을 찾을 수 없어요. 종목명을 정확히 입력하거나 종목코드(예: 005930)로 검색해보세요."}

    # 상장폐지 종목 차단: 최근 1년 주가 데이터가 없으면 거래불가 종목으로 처리
    if stock_code and stock_code not in _dead_codes:
        df_check = get_price_data(stock_code)
        if df_check is None or len(df_check) == 0:
            _dead_codes.add(stock_code)
            return {"error": f"'{corp_name}'({stock_code})은 상장폐지되었거나 거래가 중단된 종목입니다."}

    if stock_code and stock_code in _dead_codes:
        return {"error": f"'{corp_name}'({stock_code})은 상장폐지되었거나 거래가 중단된 종목입니다."}

    ip = "unknown"; device = "PC"; referrer = ""
    if request:
        ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "unknown")
        ua = request.headers.get("user-agent", "")
        referrer = request.headers.get("referer", "")
        if any(k in ua.lower() for k in ["mobile", "android", "iphone", "ipad"]):
            device = "모바일"

    cached = get_cached(corp_code)
    if cached:
        return cached

    print(f"\n🔍 분석 중: {corp_name} ({corp_code})")

    async with httpx.AsyncClient() as client:
        # ── 공시 목록 5개 기간 병렬 조회 (기존 각 함수별 개별 조회 → 한 번에) ──
        공시_results = await asyncio.gather(
            get_공시목록_async(client, corp_code, 1095),   # 3년
            get_공시목록_async(client, corp_code, 730),    # 2년
            get_공시목록_async(client, corp_code, 365),    # 1년
            get_공시목록_async(client, corp_code, 180),    # 6개월
            get_공시목록_async(client, corp_code, 30),     # 1개월
        )
        공시목록_3년, 공시목록_2년, 공시목록_1년, 공시목록_6개월, 공시목록_1개월 = 공시_results

        # ── 핵심 분석 4개 + 시장구분 병렬 실행 ──
        corp_info_r, s1_r, s2_r, s3_r = await asyncio.gather(
            get_stock_market(client, corp_code),
            calc_funding(client, corp_code, 공시목록_1년),
            calc_trust(client, corp_code, 공시목록_2년, 공시목록_6개월),
            calc_insider(client, corp_code, stock_code, 공시목록_1개월),
        )
        market, is_financial = corp_info_r
        s4_r = await calc_financial(client, corp_code, is_financial)

    s4, 재무결과, 재무위험, 자본금, 자본총계 = s4_r
    s1, cb수, cb목록, 조합수, 발행금액, 제3자유증 = s1_r
    s2, 불성실수, 번복수, 번복목록 = s2_r
    s3, 매도목록, 매도주식수, 매도비율 = s3_r

    즉사여부, 즉사메시지목록, krx_상태 = check_instant_death(공시목록_2년, stock_code, 자본금, 자본총계)
    s5, 주주변경횟수, 주주위험 = calc_ownership(공시목록_3년)
    s6, 감사위험 = calc_audit_risk(공시목록_2년)
    s7, 주가이상_위험 = calc_price_anomaly(stock_code, krx_상태, 공시목록_1개월)

    전체위험 = 재무위험 + 주주위험 + 감사위험 + 주가이상_위험
    if 번복목록:
        for t in 번복목록[:3]:
            전체위험.insert(0, f"🔄 공시 번복/정정: {t}")
    if 제3자유증 >= 1:
        전체위험.insert(0, f"제3자 배정 유상증자 {제3자유증}회")

    관리종목_감지 = (
        krx_상태.get("관리종목", False) or
        any(kw in " ".join(감사위험) for kw in ["관리종목", "거래실적", "상장적격성"]) or
        any("관리종목" in w for w in 재무위험)
    )
    if 관리종목_감지:
        전체위험.insert(0, "⚠️ 관리종목 지정 — 거래소 공식 경고 상태")

    if 즉사여부:
        total = 95
        for msg in 즉사메시지목록:
            전체위험.insert(0, f"💀 즉사 판정: {msg}")
        verdict = "위험"
    elif 관리종목_감지:
        total = max(80, min(120, s1 + s2 + s3 + s4 + s5 + s6 + s7))
        verdict = "위험"
    else:
        total = min(120, s1 + s2 + s3 + s4 + s5 + s6 + s7)
        verdict = "위험" if total >= 70 else "주의" if total >= 50 else "경계" if total >= 20 else "안전"

    print(f"✅ {corp_name}: 자금{s1}+신뢰{s2}+내부자{s3}+재무{s4}+주주{s5}+감사{s6}+주가이상{s7} = {total}점 [{verdict}]")

    공시_timeline = sorted(공시목록_1년, key=lambda x: x.get("rcept_dt", ""), reverse=True)[:30]

    result = {
        "종목": corp_name, "corp_code": corp_code, "stock_code": stock_code,
        "market": market,
        "score": total, "s1": s1, "s2": s2, "s3": s3, "s4": s4, "s5": s5, "s6": s6, "s7": s7,
        "즉사판정": 즉사여부, "관리종목": 관리종목_감지,
        "cb_count": cb수, "cb_list": [c["title"] for c in cb목록],
        "제3자유증": 제3자유증, "투자조합_건수": 조합수, "총발행금액": 발행금액,
        "불성실_count": 불성실수, "번복_count": 번복수, "번복_list": 번복목록,
        "매도_list": 매도목록, "매도_주식수": 매도주식수, "매도비율": round(매도비율, 2),
        "급락률": krx_상태.get("급락률", 0),
        "재무분석": 재무결과, "재무위험항목": 전체위험, "verdict": verdict,
        "공시목록": [
            {
                "date": c.get("rcept_dt", ""),
                "title": c.get("report_nm", ""),
                "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={c.get('rcept_no', '')}"
            }
            for c in 공시_timeline
        ],
    }

    set_cached(corp_code, result)

    global _analyzed_cache_list
    _analyzed_cache_list = [s for s in _analyzed_cache_list if s["code"] != stock_code]
    _analyzed_cache_list.append({"name": corp_name, "code": stock_code, "score": total, "verdict": verdict})
    if len(_analyzed_cache_list) > 100:
        _analyzed_cache_list.pop(0)

    return result


@app.get("/price")
def get_price(stock_code: str = ""):
    try:
        오늘 = datetime.now().strftime("%Y%m%d")
        일년전 = (datetime.now() - timedelta(days=365)).strftime("%Y%m%d")
        df = krx.get_market_ohlcv_by_date(일년전, 오늘, stock_code)
        if df is None or len(df) == 0:
            return {"error": "주가 데이터 없음"}
        최근 = df.iloc[-1]
        전일 = df.iloc[-2] if len(df) >= 2 else 최근
        현재가 = int(최근["종가"]); 전일종가 = int(전일["종가"])
        등락 = 현재가 - 전일종가; 등락률 = 등락 / 전일종가 * 100

        def chart(days):
            sl = df.tail(days)
            return {
                "labels": [d.strftime("%m/%d") for d in sl.index],
                "prices": [int(v) for v in sl["종가"]]
            }

        return {
            "현재가": 현재가, "전일종가": 전일종가,
            "등락": 등락, "등락률": round(등락률, 2),
            "고가": int(최근["고가"]), "저가": int(최근["저가"]),
            "거래량": int(최근["거래량"]),
            "52주고": int(df["고가"].max()), "52주저": int(df["저가"].min()),
            "날짜": df.index[-1].strftime("%Y.%m.%d"),
            "chart": {"1m": chart(22), "3m": chart(66), "6m": chart(132), "1y": chart(252)},
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/search")
def search_autocomplete(q: str = ""):
    if not CORP_LIST_READY or not q:
        return []
    q = q.strip()
    results = []
    seen = set()
    # 코드 완전 일치 우선
    for c in CORP_LIST:
        if c["stock_code"] == q and c["stock_code"] not in seen:
            results.append({"name": c["corp_name"], "code": c["stock_code"]})
            seen.add(c["stock_code"])
    # 이름 포함 검색 (활성 종목만)
    for c in CORP_LIST:
        if q in c["corp_name"] and c["stock_code"] not in seen and _is_active(c["stock_code"]):
            results.append({"name": c["corp_name"], "code": c["stock_code"]})
            seen.add(c["stock_code"])
        if len(results) >= 8:
            break
    return results[:8]


@app.get("/news")
def get_news(stock_code: str = ""):
    try:
        url = f"https://finance.naver.com/item/news_news.naver?code={stock_code}&page=1&sm=title_entity_id.basic&clusterId="
        r = sync_requests.get(url, headers=_NAVER_HEADERS, timeout=8)
        r.encoding = "euc-kr"
        html = r.text
        rows = re.findall(
            r'href="(/item/news_read\.naver\?article_id=\d+&office_id=\d+[^"]*)"[^>]*class="tit"[^>]*>([^<]+)</a>.*?'
            r'<td class="info">([^<]+)</td>.*?'
            r'<td class="date">([^<]+)</td>',
            html, re.DOTALL
        )
        bad_kw = ["하락","적자","손실","횡령","배임","수사","폐지","위기","급락","부도","파산","소송","규제","제재","경고","정지","조사","검찰"]
        good_kw = ["상승","흑자","성장","수주","계약","출시","호재","강세","급등","신규","협약","이익","턴어라운드","매출"]
        news = []
        for href, title, press, date in rows[:30]:
            title = re.sub(r'&\w+;', '', title).strip()
            press = press.strip(); date = date.strip()
            link = "https://finance.naver.com" + href.strip()
            bad = sum(1 for k in bad_kw if k in title)
            good = sum(1 for k in good_kw if k in title)
            if bad == 0 and good == 0:
                continue
            ntype = "bad" if bad >= good else "good"
            news.append({"type": ntype, "title": title, "link": link, "press": press, "date": date})
        good_list = [n for n in news if n["type"] == "good"][:3]
        bad_list  = [n for n in news if n["type"] == "bad"][:3]
        return {"good": good_list, "bad": bad_list}
    except Exception as e:
        return {"error": str(e), "good": [], "bad": []}


@app.get("/warning-stocks")
def get_warning_stocks(exclude: str = ""):
    danger = [s for s in _analyzed_cache_list if s["code"] != exclude and s["score"] >= 50]
    seen = set(); unique = []
    for s in reversed(danger):
        if s["code"] not in seen:
            seen.add(s["code"]); unique.append(s)
    unique.sort(key=lambda x: x["score"], reverse=True)
    return unique[:5]


_top_movers_cache = {"ts": 0, "data": None}
_NAVER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://finance.naver.com/",
}

def _parse_naver_sise(sosok: str, direction: str):
    """sosok: '0'=KOSPI, '1'=KOSDAQ / direction: 'rise' or 'fall'"""
    url = f"https://finance.naver.com/sise/sise_{direction}.naver?sosok={sosok}"
    try:
        r = sync_requests.get(url, headers=_NAVER_HEADERS, timeout=8)
        r.encoding = "euc-kr"
        html = r.text
        rows = re.findall(
            r'code=(\d{6})[^>]*class="tltle">([^<]+)</a></td>\s*'
            r'<td class="number">([\d,]+)</td>.*?'
            r'([+\-]?[\d.]+)%',
            html, re.DOTALL
        )
        items = []
        for code, name, price_str, rate_str in rows[:10]:
            try:
                price = int(price_str.replace(",", ""))
                rate = float(rate_str)
                if direction == "fall" and rate > 0:
                    rate = -rate
            except Exception:
                price, rate = 0, 0.0
            items.append({"code": code, "name": name.strip(), "rate": rate, "price": price})
        return items
    except Exception as e:
        print(f"naver sise {direction}/{sosok} 오류: {e}")
        return []

@app.get("/top-movers")
def get_top_movers():
    global _top_movers_cache
    now = time.time()
    if now - _top_movers_cache["ts"] < 300 and _top_movers_cache["data"]:
        return _top_movers_cache["data"]
    try:
        kospi_up   = _parse_naver_sise("0", "rise")
        kosdaq_up  = _parse_naver_sise("1", "rise")
        kospi_dn   = _parse_naver_sise("0", "fall")
        kosdaq_dn  = _parse_naver_sise("1", "fall")

        상승_raw = kospi_up + kosdaq_up
        하락_raw = kospi_dn + kosdaq_dn
        상승_raw.sort(key=lambda x: x["rate"], reverse=True)
        하락_raw.sort(key=lambda x: x["rate"])

        상승 = 상승_raw[:10]
        하락 = 하락_raw[:10]
        if not 상승 and not 하락:
            return {"error": "데이터 없음", "상승": [], "하락": []}
        today_str = datetime.now().strftime("%m/%d")
        result = {"상승": 상승, "하락": 하락, "date": today_str}
        _top_movers_cache = {"ts": now, "data": result}
        return result
    except Exception as e:
        print(f"top-movers 오류: {e}")
        return {"error": str(e), "상승": [], "하락": []}


@app.get("/news-debug")
def get_news_debug(stock_code: str = "005930"):
    try:
        url = f"https://finance.naver.com/item/news.naver?code={stock_code}&page=1"
        r = sync_requests.get(url, headers=_NAVER_HEADERS, timeout=8)
        r.encoding = "euc-kr"
        html = r.text
        logs = {}
        for test_url in [
            f"https://finance.naver.com/item/news_news.naver?code={stock_code}&page=1&sm=title_entity_id.basic&clusterId=",
            f"https://finance.naver.com/item/news.naver?code={stock_code}",
        ]:
            r2 = sync_requests.get(test_url, headers=_NAVER_HEADERS, timeout=8)
            r2.encoding = "euc-kr"
            h = r2.text
            idx = h.find("article_id")
            idx2 = h.find("office_id")
            idx3 = h.find("<td class")
            logs[test_url[-50:]] = {
                "status": r2.status_code, "len": len(h),
                "article_id_at": h[idx:idx+200] if idx >= 0 else "NOT FOUND",
                "td_class_at": h[idx3:idx3+300] if idx3 >= 0 else "NOT FOUND",
            }
        return logs
    except Exception as e:
        return {"error": str(e)}


@app.get("/top-movers-debug")
def get_top_movers_debug():
    logs = []
    url = "https://finance.naver.com/sise/sise_rise.naver?sosok=0"
    try:
        r = sync_requests.get(url, headers=_NAVER_HEADERS, timeout=8)
        r.encoding = "euc-kr"
        html = r.text
        result = _parse_naver_sise("0", "rise")
        logs.append(f"parsed {len(result)} items")
        logs.append(f"sample: {result[:3]}")
    except Exception as e:
        logs.append(f"ERROR: {e}")
    return {"logs": logs}


@app.get("/investor")
def get_investor(stock_code: str):
    """네이버 금융 외국인/기관 순매매 5일치 스크래핑"""
    url = f"https://finance.naver.com/item/frgn.naver?code={stock_code}&page=1"
    try:
        r = sync_requests.get(url, headers=_NAVER_HEADERS, timeout=8)
        html = r.text
        rows = re.findall(
            r'<span class="tah p10 gray03">(\d{4}\.\d{2}\.\d{2})</span>'
            r'.*?'
            r'<span class="tah p11[^"]*">([+\-][\d,]+)</span>'   # 기관 순매매
            r'.*?'
            r'<span class="tah p11[^"]*">([+\-][\d,]+)</span>'   # 외국인 순매매
            r'.*?'
            r'<span class="tah p11">([\d.]+)%</span>',            # 외국인 보유율
            html, re.DOTALL
        )
        result = []
        for row in rows[:5]:
            try:
                date, inst, frgn, frgn_rate = row
                result.append({
                    "date": date,
                    "institution": int(inst.replace(",", "").replace("+", "")),
                    "foreign": int(frgn.replace(",", "").replace("+", "")),
                    "foreign_rate": float(frgn_rate),
                })
            except Exception:
                continue
        if not result:
            return {"error": "데이터 파싱 실패", "days": []}
        return {"days": result}
    except Exception as e:
        return {"error": str(e), "days": []}


@app.get("/investor-debug")
def get_investor_debug(stock_code: str = "005930"):
    return get_investor(stock_code)


@app.post("/log")
async def log_session(request: Request):
    try:
        data = await request.json()
        ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "unknown")
        ua = request.headers.get("user-agent", "")
        ua_lower = ua.lower()
        if "iphone" in ua_lower or "ipad" in ua_lower:
            device = "iOS"
        elif "android" in ua_lower:
            device = "Android"
        elif "windows" in ua_lower:
            device = "Windows"
        elif "mac" in ua_lower:
            device = "macOS"
        elif "mobile" in ua_lower:
            device = "모바일(기타)"
        else:
            device = "PC(기타)"
        referrer = request.headers.get("referer", "")
        log_type = data.get("type", "usage")

        region = ""
        try:
            async with httpx.AsyncClient() as client:
                geo = await client.get(
                    f"http://ip-api.com/json/{ip}?lang=ko&fields=city,regionName", timeout=3)
                geo_data = geo.json()
                region = f"{geo_data.get('regionName', '')} {geo_data.get('city', '')}".strip()
        except: pass

        company = data.get("company", "")
        log_to_sheets({
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "type": "사용" if company else log_type,
            "company": company,
            "stock_code": data.get("stock_code", ""),
            "score": data.get("score", 0),
            "ip": ip,
            "device": device,
            "region": region,
            "referrer": referrer,
            "session_time": data.get("session_time", 0),
            "cached": False,
        })
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}
