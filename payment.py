"""支付与充值：下单 + 幂等回调 + 支付宝当面付接入。

流程：
    用户充值 → create_order（支付宝当面付 alipay.trade.precreate 拿二维码）
             → 扫码付款 → 支付宝异步回调 /api/recharge/callback
             → verify_callback_signature 验签 → mark_paid 幂等充值

未配置支付宝（ALIPAY_APP_ID 为空）时，create_order 走「模拟支付」兜底，
返回空二维码，前端沿用旧的 /api/recharge/simulate 流程。
"""
import base64
import json
import os
import time
import uuid
from datetime import datetime

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

import db

ORDER_EXPIRE_SECONDS = 2 * 3600  # 订单 2 小时未支付即作废

ALIPAY_GATEWAY = "https://openapi.alipay.com/gateway.do"
ALIPAY_APP_ID = os.getenv("ALIPAY_APP_ID", "").strip()
ALIPAY_PRIVATE_KEY = os.getenv("ALIPAY_PRIVATE_KEY", "").strip()
ALIPAY_PUBLIC_KEY = os.getenv("ALIPAY_PUBLIC_KEY", "").strip()
ALIPAY_NOTIFY_URL = os.getenv("ALIPAY_NOTIFY_URL", "").strip()


def alipay_configured():
    return bool(ALIPAY_APP_ID and ALIPAY_PRIVATE_KEY and ALIPAY_PUBLIC_KEY)


def _gen_out_trade_no():
    return f"VN{int(time.time() * 1000)}{uuid.uuid4().hex[:6].upper()}"


# ---------- RSA2 签名 / 验签 ----------
def _wrap_pem(key_str, label):
    key_str = (key_str or "").strip()
    if not key_str:
        return ""
    if "-----BEGIN" in key_str:
        return key_str
    body = "\n".join(key_str[i:i + 64] for i in range(0, len(key_str), 64))
    return f"-----BEGIN {label}-----\n{body}\n-----END {label}-----\n"


def _build_sign_content(params):
    items = [(k, params[k]) for k in sorted(params) if params.get(k) not in (None, "")]
    return "&".join(f"{k}={v}" for k, v in items)


def _sign_rsa2(content, private_key_pem):
    key = serialization.load_pem_private_key(
        _wrap_pem(private_key_pem, "RSA PRIVATE KEY").encode(), password=None
    )
    sig = key.sign(content.encode(), padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(sig).decode()


def _verify_rsa2(content, signature_b64, public_key_pem):
    try:
        key = serialization.load_pem_public_key(
            _wrap_pem(public_key_pem, "PUBLIC KEY").encode()
        )
        key.verify(
            base64.b64decode(signature_b64),
            content.encode(),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return True
    except Exception:
        return False


# ---------- 支付宝当面付：预下单拿二维码 ----------
def _alipay_precreate(out_trade_no, amount):
    biz_content = json.dumps(
        {
            "out_trade_no": out_trade_no,
            "total_amount": f"{amount:.2f}",
            "subject": "视频转笔记充值",
            "timeout_express": "2h",
        },
        ensure_ascii=False,
    )
    params = {
        "app_id": ALIPAY_APP_ID,
        "method": "alipay.trade.precreate",
        "format": "JSON",
        "charset": "utf-8",
        "sign_type": "RSA2",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "version": "1.0",
        "notify_url": ALIPAY_NOTIFY_URL,
        "biz_content": biz_content,
    }
    params["sign"] = _sign_rsa2(_build_sign_content(params), ALIPAY_PRIVATE_KEY)
    resp = requests.post(ALIPAY_GATEWAY, data=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    body = data.get("alipay_trade_precreate_response", {})
    if body.get("code") != "10000":
        raise RuntimeError(f"支付宝下单失败：{body.get('sub_msg') or body.get('msg')}")
    return body.get("qr_code", "")


def create_order(user_id, amount):
    """创建充值订单，返回 (order_id, out_trade_no, qr_code)。

    已配置支付宝时返回可扫码的 qr_code；未配置时返回空串（模拟支付兜底）。
    """
    if not user_id:
        raise ValueError("用户不存在")
    try:
        amount = round(float(amount), 2)
    except (TypeError, ValueError):
        raise ValueError("金额格式不正确")
    if amount <= 0:
        raise ValueError("金额必须大于 0")
    if amount > 1000:
        raise ValueError("单次充值不能超过 1000 元")

    out_trade_no = _gen_out_trade_no()
    qr_code = ""
    provider = ""
    if alipay_configured():
        qr_code = _alipay_precreate(out_trade_no, amount)
        provider = "alipay"

    now = time.time()
    session = db.get_session()
    try:
        order = db.Order(user_id=user_id, amount=amount, status="pending",
                         provider=provider, out_trade_no=out_trade_no,
                         transaction_id="", pay_url=qr_code,
                         created_at=now, paid_at=None,
                         expire_at=now + ORDER_EXPIRE_SECONDS)
        session.add(order)
        session.commit()
        return order.id, order.out_trade_no, qr_code
    finally:
        session.close()


def mark_paid(out_trade_no, transaction_id, provider="manual", expected_amount=None):
    """支付回调确认：幂等充值。重复回调不重复到账，返回是否本次实际充值。

    expected_amount 传入回调里的金额，与订单金额不符时拒绝入账（防篡改）。
    """
    session = db.get_session()
    try:
        order = (session.query(db.Order)
                 .filter(db.Order.out_trade_no == out_trade_no).first())
        if not order:
            return False
        if order.status == "paid":
            return False  # 幂等：已支付过，不重复充值
        if expected_amount is not None:
            try:
                if abs(float(expected_amount) - float(order.amount)) > 0.001:
                    return False  # 金额不符，拒绝入账
            except (TypeError, ValueError):
                return False
        order.status = "paid"
        order.transaction_id = transaction_id
        order.provider = provider
        order.paid_at = time.time()
        user = session.get(db.User, order.user_id)
        if user:
            user.balance = round(user.balance + order.amount, 2)
        session.add(db.Billing(user_id=order.user_id, amount=order.amount,
                               type="recharge", created_at=time.time()))
        session.commit()
        return True
    finally:
        session.close()


def verify_callback_signature(provider, request_data):
    """校验第三方支付回调签名。返回 True/False。"""
    if provider == "alipay":
        sign = request_data.get("sign", "")
        if not sign:
            return False
        params = {k: v for k, v in request_data.items()
                  if k not in ("sign", "sign_type")}
        return _verify_rsa2(_build_sign_content(params), sign, ALIPAY_PUBLIC_KEY)
    raise NotImplementedError(f"{provider} 回调验签尚未接入")


def qr_to_data_url(text):
    """把二维码字符串（当面付 qr_code）转成可直接 <img> 展示的 SVG data URL。"""
    if not text:
        return ""
    try:
        import segno
        svg = segno.make(text, error="M").svg_inline(scale=6, border=2)
        return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()
    except Exception:
        return ""
