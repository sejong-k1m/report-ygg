"""
카카오톡 OAuth 첫 토큰 발급 스크립트.

사용:
    python tools/kakao_auth.py

흐름:
1. 카카오 로그인 페이지가 브라우저에서 열림
2. 본인 카카오 계정으로 로그인 + 동의
3. localhost:8080 으로 리다이렉트 → 인가코드 자동 수신
4. 인가코드로 access_token / refresh_token 발급
5. data/kakao_tokens.json 에 저장 완료

준비물 (카카오 디벨로퍼스 https://developers.kakao.com 에서):
- 앱 생성
- 플랫폼 → Web → 사이트 도메인: http://localhost:8080
- 카카오 로그인 활성화 ON
- Redirect URI: http://localhost:8080
- 동의항목 → 카카오톡 메시지 전송 (talk_message) 활성화
- "REST API 키" 복사 → app_secrets.py 의 KAKAO_REST_API_KEY 에 저장

refresh_token 만료 (60일) 시 이 스크립트를 다시 실행하면 됩니다.
"""
from __future__ import annotations

import http.server
import json
import socketserver
import sys
import time
import urllib.parse
import webbrowser
from pathlib import Path

# 프로젝트 루트를 path에 추가 (tools/ 에서 실행 시)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests

import app_secrets

REDIRECT_URI = "http://localhost:8080"
TOKEN_PATH = ROOT / "data" / "kakao_tokens.json"
KAKAO_AUTH_URL = "https://kauth.kakao.com/oauth/authorize"
KAKAO_TOKEN_URL = "https://kauth.kakao.com/oauth/token"


class _CodeHandler(http.server.BaseHTTPRequestHandler):
    received_code: str = ""
    received_error: str = ""

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        if "code" in qs:
            _CodeHandler.received_code = qs["code"][0]
            self._send_html(
                "<h1>✅ 카카오 인증 완료</h1>"
                "<p>이 창을 닫고 터미널로 돌아가세요.</p>"
            )
        elif "error" in qs:
            _CodeHandler.received_error = qs.get("error_description", ["unknown"])[0]
            self._send_html(f"<h1>❌ 인증 실패</h1><p>{_CodeHandler.received_error}</p>", status=400)
        else:
            self._send_html("<h1>대기 중...</h1>")

    def log_message(self, format, *args):
        pass  # silence default access log

    def _send_html(self, body: str, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        html = f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>Kakao Auth</title></head><body>{body}</body></html>"
        self.wfile.write(html.encode("utf-8"))


def main():
    rest_key = getattr(app_secrets, "KAKAO_REST_API_KEY", "").strip()
    if not rest_key or "..." in rest_key:
        print("❌ app_secrets.py 에 KAKAO_REST_API_KEY 가 설정되지 않았습니다.")
        print("   카카오 디벨로퍼스 → 내 애플리케이션 → 앱 키 → 'REST API 키' 복사")
        sys.exit(1)

    auth_url = (
        f"{KAKAO_AUTH_URL}"
        f"?response_type=code"
        f"&client_id={rest_key}"
        f"&redirect_uri={urllib.parse.quote(REDIRECT_URI)}"
        f"&scope=talk_message"
    )

    print("=" * 70)
    print("카카오 인증 시작")
    print("=" * 70)
    print(f"\n브라우저에서 카카오 로그인을 진행하세요.")
    print(f"브라우저가 자동으로 열리지 않으면 아래 URL을 직접 여세요:\n")
    print(auth_url)
    print()

    # localhost:8080 에서 콜백 대기
    try:
        with socketserver.TCPServer(("localhost", 8080), _CodeHandler) as httpd:
            webbrowser.open(auth_url)
            print("⏳ localhost:8080 에서 인가코드 대기 중...\n")
            while not _CodeHandler.received_code and not _CodeHandler.received_error:
                httpd.handle_request()
    except OSError as e:
        print(f"❌ 8080 포트를 열 수 없음: {e}")
        print("   다른 프로그램이 8080을 쓰고 있을 수 있음. 종료 후 재시도.")
        sys.exit(1)

    if _CodeHandler.received_error:
        print(f"❌ 카카오 인증 실패: {_CodeHandler.received_error}")
        sys.exit(1)

    code = _CodeHandler.received_code
    print(f"✅ 인가코드 수신 완료 (앞 10자: {code[:10]}...)")

    # 인가코드 → access/refresh token 교환
    print("⏳ 토큰 발급 중...")
    r = requests.post(KAKAO_TOKEN_URL, data={
        "grant_type": "authorization_code",
        "client_id": rest_key,
        "redirect_uri": REDIRECT_URI,
        "code": code,
    }, timeout=10)
    if r.status_code != 200:
        print(f"❌ 토큰 발급 실패: {r.status_code} {r.text}")
        sys.exit(1)

    body = r.json()
    now = int(time.time())
    saved = {
        "access_token": body["access_token"],
        "access_expires_at": now + int(body.get("expires_in", 21600)) - 60,
        "refresh_token": body["refresh_token"],
        "refresh_expires_at": now + int(body.get("refresh_token_expires_in", 60 * 24 * 3600)) - 60,
    }
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(json.dumps(saved, indent=2, ensure_ascii=False), encoding="utf-8")

    print()
    print("=" * 70)
    print(f"✅ 토큰 저장 완료: {TOKEN_PATH}")
    print(f"   access_token 만료: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(saved['access_expires_at']))} (자동 갱신됨)")
    print(f"   refresh_token 만료: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(saved['refresh_expires_at']))} (만료 시 이 스크립트 재실행)")
    print("=" * 70)

    # 발송 테스트
    print("\n📨 테스트 메시지 발송 중...")
    test = {
        "object_type": "text",
        "text": "[⚙️시스템] 카카오 알림 연결 테스트 성공 🎉\n연기금 자동매매 봇이 이 채널로 알림을 보냅니다.",
        "link": {
            "web_url": "https://www.kiwoom.com",
            "mobile_web_url": "https://www.kiwoom.com",
        },
    }
    r2 = requests.post(
        "https://kapi.kakao.com/v2/api/talk/memo/default/send",
        headers={"Authorization": f"Bearer {saved['access_token']}"},
        data={"template_object": json.dumps(test, ensure_ascii=False)},
        timeout=5,
    )
    if r2.status_code < 300:
        print("✅ 카톡 '나에게 보내기' 도착 확인하세요.")
    else:
        print(f"⚠️ 테스트 발송 실패: {r2.status_code} {r2.text[:200]}")
        print("   토큰은 저장되었으나 메시지 권한(talk_message)이 없을 수 있음.")
        print("   카카오 디벨로퍼스 → 동의항목 → '카카오톡 메시지 전송' 활성화 확인.")


if __name__ == "__main__":
    main()
