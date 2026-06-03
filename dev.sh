#!/bin/bash
set -e

# .env 로드
if [ -f .env ]; then
  export $(grep -v '^#' .env | xargs)
fi

# 의존성 설치 (처음 한 번만)
pip install -q -r requirements.txt

echo ""
echo "  API  → http://localhost:8000"
echo "  앱   → http://localhost:3000"
echo ""

# 프론트 정적 서버 (포트 3000) 백그라운드 실행
python3 -m http.server 3000 &
FRONTEND_PID=$!

# Ctrl+C 시 프론트 서버도 같이 종료
trap "kill $FRONTEND_PID 2>/dev/null; exit" INT TERM

# API 서버 실행 (포트 8000, 자동 리로드)
uvicorn server:app --reload --host 0.0.0.0 --port 8000
