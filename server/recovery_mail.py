"""Fixed-destination transactional mail. No passwords, retries or configurable URLs."""

import hashlib
import hmac
import json
import time
from datetime import UTC, datetime

import httpx

HOST = "ses.tencentcloudapi.com"


class DeliveryUnavailable(Exception):
    """Do not attach provider bodies, credentials or codes to this exception."""


def signed_headers(secret_id, secret_key, region, payload, timestamp):
    """Tencent Cloud TC3 request signature, using the exact outgoing UTF-8 bytes."""
    date = datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d")
    content_type = "application/json; charset=utf-8"
    signed = "content-type;host;x-tc-action"
    canonical = "\n".join(
        [
            "POST",
            "/",
            "",
            f"content-type:{content_type}\nhost:{HOST}\nx-tc-action:sendemail\n",
            signed,
            hashlib.sha256(payload).hexdigest(),
        ]
    )
    scope = f"{date}/ses/tc3_request"
    message = f"TC3-HMAC-SHA256\n{timestamp}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}"
    key = ("TC3" + secret_key).encode()
    for part in (date, "ses", "tc3_request"):
        key = hmac.digest(key, part.encode(), "sha256")
    signature = hmac.new(key, message.encode(), "sha256").hexdigest()
    return {
        "Content-Type": content_type,
        "Host": HOST,
        "X-TC-Action": "SendEmail",
        "X-TC-Version": "2020-10-02",
        "X-TC-Timestamp": str(timestamp),
        "X-TC-Region": region,
        "Authorization": f"TC3-HMAC-SHA256 Credential={secret_id}/{scope}, SignedHeaders={signed}, Signature={signature}",
    }


def send_recovery_code(settings, code):
    """Provider acceptance is not proof of inbox delivery. Caller records this distinction."""
    payload = json.dumps(
        {
            "FromEmailAddress": settings.sender,
            "Destination": [settings.recipient],
            "Subject": "Prism 管理员密码重置验证码",
            "Template": {
                "TemplateID": settings.template_id,
                "TemplateData": json.dumps({"code": code}, separators=(",", ":")),
            },
            "TriggerType": 1,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    headers = signed_headers(
        settings.secret_id, settings.secret_key, settings.region, payload, int(time.time())
    )
    try:
        # Default TLS verification; fixed host, no redirects, no ambient proxies or retries.
        with httpx.Client(timeout=10, trust_env=False, follow_redirects=False) as client:
            response = client.post(f"https://{HOST}/", content=payload, headers=headers)
        response.raise_for_status()
        result = response.json().get("Response", {})
        if result.get("Error") or not result.get("MessageId"):
            raise DeliveryUnavailable()
    except Exception:
        # Even transport errors may carry a request with its authentication header.
        raise DeliveryUnavailable() from None
