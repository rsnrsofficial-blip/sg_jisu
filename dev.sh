#!/bin/bash
set -e

# .env 로드
if [ -f .env ]; then
  export $(grep -v '^#' .env | xargs)
fi

# 의존성 설치 (처음 한 번만)
pip install -q -r requirements.txt

# 로컬 서버 실행 (포트 8000, 자동 리로드)
uvicorn server:app --reload --host 0.0.0.0 --port 8000
