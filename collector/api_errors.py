"""Structured API diagnostics. Never inspect headers, request bodies or key values."""
import re
import os
from datetime import datetime, timezone
import anthropic


def safe_message(value):
    text = str(value)
    key = os.environ.get('ANTHROPIC_API_KEY')
    if key:
        text = text.replace(key, '[REDACTED]')
    text = re.sub(r"sk-[A-Za-z0-9_-]+", "[REDACTED]", text)
    text = re.sub(r"(?i)(bearer\s+|(?:x-api-key|api[_-]?key|token|authorization)\s*[:=]\s*)[^\s,;]+", r"\1[REDACTED]", text)
    return text[:500]


def classify(error):
    status = getattr(error, "status_code", None)
    body = getattr(error, "body", None)
    detail = body.get("error", body) if isinstance(body, dict) else {}
    if not isinstance(detail, dict):
        detail = {}
    message = safe_message(detail.get("message") or getattr(error, "message", None) or type(error).__name__)
    kind = str(detail.get("type") or type(error).__name__)
    text = message.lower()
    category = "unknown"
    if isinstance(error, anthropic.APIStatusError):
        if status == 401:
            category = "authentication"
        elif status == 403:
            category = "permission"
        elif re.search(r"spend(?:ing)?[ _-]*(?:limit|cap)|monthly.{0,25}(?:limit|cap)|usage limit", text):
            category = "spend-limit"
        elif re.search(r"credit balance.{0,30}(?:too low|insufficient|exhausted)|insufficient credits?|out of credits", text):
            category = "credit-exhausted"
        elif status == 402 or kind == "billing_error":
            category = "billing"
        elif status == 429:
            category = "rate-limit"
        elif status and status >= 500:
            category = "provider-unavailable"
        elif status == 400:
            category = "invalid-request"
    elif isinstance(error, anthropic.APIConnectionError):
        category = "connection"
    request_id = str(getattr(error, "request_id", "") or "")
    return {"category": category, "http_status": status, "error_type": kind[:80], "message": message,
            "request_id": request_id if re.fullmatch(r"req_[A-Za-z0-9_-]{1,100}", request_id) else None,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


HALT = {"credit-exhausted", "billing", "spend-limit", "authentication", "permission", "rate-limit"}
LABELS = {"credit-exhausted": "API가 해당 키의 크레딧 부족을 보고했습니다(크레딧 소진 여부·연결 조직 확인 필요)",
          "billing": "API 결제 접근 오류(잔액 소진으로 단정할 수 없음)",
          "spend-limit": "API 조직·워크스페이스 또는 사용 등급 지출 한도 도달",
          "authentication": "API 키 인증 실패", "permission": "API 키·조직·워크스페이스 접근 권한 오류",
          "rate-limit": "API 요청 속도 제한"}


def notice(diagnostic):
    return (LABELS.get(diagnostic["category"], "API 요청 오류") +
            f" · HTTP {diagnostic['http_status']} / {diagnostic['error_type']}. " +
            "AI 단계만 보류하며 대기 항목과 기존 발송은 유지합니다. GitHub 예약 지연과는 별개입니다. 카드 등록·충전·자동 충전은 하지 않습니다.")
