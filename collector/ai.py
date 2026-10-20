"""AI 호출을 이 파일 한 곳에 모은다(설계안: 모델·경로 전환이 설정 변경으로 끝나게).

0단계에서는 키 점검만 한다. 판정(1단계 이후)도 이 파일에 함수를 추가해서 쓴다.
키는 GitHub Actions 비밀값 ANTHROPIC_API_KEY (colorado-alerts 워크스페이스 키)에서만 읽는다.
"""
import os

import anthropic


def get_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def check_key() -> dict:
    """모델 목록 조회로 키가 살아 있는지 확인한다. 토큰을 쓰지 않는다."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {"ok": False, "status": "missing", "detail": "GitHub secret ANTHROPIC_API_KEY is empty"}
    try:
        page = get_client().models.list(limit=1)
        first = page.data[0].id if page.data else "-"
        return {"ok": True, "status": "ok", "detail": f"models.list ok ({first})"}
    except anthropic.AuthenticationError as e:
        return {"ok": False, "status": "invalid", "detail": f"401 {e.message}"[:300]}
    except anthropic.PermissionDeniedError as e:
        return {"ok": False, "status": "permission", "detail": f"403 {e.message}"[:300]}
    except anthropic.RateLimitError:
        return {"ok": False, "status": "transient", "detail": "429 rate limited"}
    except anthropic.APIStatusError as e:
        return {"ok": False, "status": "transient", "detail": f"{e.status_code} {e.message}"[:300]}
    except anthropic.APIConnectionError:
        return {"ok": False, "status": "transient", "detail": "network error"}
