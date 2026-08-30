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
from urllib.parse import urlencode

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
ALIPAY_RETURN_URL = os.getenv("ALIPAY_RETURN_URL", "").strip() or (
    ALIPAY_NOTIFY_URL.rsplit("/api/", 1)[0] + "/" if ALIPAY_NOTIFY_URL else ""
)


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


def _load_private_key(private_key_pem):
    pem = (private_key_pem or "").strip()
    if not pem:
        raise ValueError("应用私钥为空")
    if "-----BEGIN" in pem:
        return serialization.load_pem_private_key(pem.encode(), password=None)
    der = base64.b64decode(pem)
    return serialization.load_der_private_key(der, password=None)


def _sign_rsa2(content, private_key_pem):
    key = _load_private_key(private_key_pem)
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


# ---------- 支付宝网站支付：构造跳转 URL（PC 电脑网站支付 / 手机网站支付）----------
def _alipay_pay_url(out_trade_no, amount, is_pc):
    method = "alipay.trade.page.pay" if is_pc else "alipay.trade.wap.pay"
    product_code = "FAST_INSTANT_TRADE_PAY" if is_pc else "QUICK_WAP_WAY"
    biz = {
        "out_trade_no": out_trade_no,
        "total_amount": f"{amount:.2f}",
        "subject": "视频转笔记充值",
        "product_code": product_code,
    }
    if not is_pc:
        biz["quit_url"] = ALIPAY_RETURN_URL
    biz_content = json.dumps(biz, ensure_ascii=True)
    params = {
        "app_id": ALIPAY_APP_ID,
        "method": method,
        "format": "JSON",
        "charset": "utf-8",
        "sign_type": "RSA2",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "version": "1.0",
        "notify_url": ALIPAY_NOTIFY_URL,
        "return_url": ALIPAY_RETURN_URL,
        "biz_content": biz_content,
    }
    params["sign"] = _sign_rsa2(_build_sign_content(params), ALIPAY_PRIVATE_KEY)
    return ALIPAY_GATEWAY + "?" + urlencode(params)


def create_order(user_id, amount, is_pc=True):
    """创建充值订单，返回 (order_id, out_trade_no, pay_url)。

    已配置支付宝时返回网站支付跳转 URL（PC 电脑网站支付 / 手机网站支付）；未配置时返回空串（模拟支付兜底）。
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
    pay_url = ""
    provider = ""
    if alipay_configured():
        pay_url = _alipay_pay_url(out_trade_no, amount, is_pc)
        provider = "alipay"

    now = time.time()
    session = db.get_session()
    try:
        order = db.Order(user_id=user_id, amount=amount, status="pending",
                         provider=provider, out_trade_no=out_trade_no,
                         transaction_id="", pay_url=pay_url,
                         created_at=now, paid_at=None,
                         expire_at=now + ORDER_EXPIRE_SECONDS)
        session.add(order)
        session.commit()
        print(f"[下单] user_id={user_id} amount={amount} out_trade_no={out_trade_no} provider={provider or 'manual'}", flush=True)
        return order.id, order.out_trade_no, pay_url
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
            print(f"[mark_paid] 订单不存在 out_trade_no={out_trade_no}", flush=True)
            return False
        if order.status == "paid":
            print(f"[mark_paid] 订单已支付（幂等） out_trade_no={out_trade_no}", flush=True)
            return False
        if expected_amount is not None:
            try:
                if abs(float(expected_amount) - float(order.amount)) > 0.001:
                    print(f"[mark_paid] 金额不符 expected={expected_amount} order={order.amount} out_trade_no={out_trade_no}", flush=True)
                    return False
            except (TypeError, ValueError):
                print(f"[mark_paid] 金额解析失败 expected={expected_amount} out_trade_no={out_trade_no}", flush=True)
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
        print(f"[mark_paid] 充值成功 user_id={order.user_id} amount={order.amount} out_trade_no={out_trade_no}", flush=True)
        return True
    finally:
        session.close()


def refund(out_trade_no, refund_amount, refund_reason="用户申请退款"):
    """调用支付宝退款接口（同步返回结果）。返回 (ok, message)。"""
    out_request_no = f"RF{int(time.time() * 1000)}{uuid.uuid4().hex[:6].upper()}"
    biz_content = json.dumps(
        {
            "out_trade_no": out_trade_no,
            "refund_amount": f"{refund_amount:.2f}",
            "refund_reason": refund_reason,
            "out_request_no": out_request_no,
        },
        ensure_ascii=True,
    )
    params = {
        "app_id": ALIPAY_APP_ID,
        "method": "alipay.trade.refund",
        "format": "JSON",
        "charset": "utf-8",
        "sign_type": "RSA2",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "version": "1.0",
        "biz_content": biz_content,
    }
    params["sign"] = _sign_rsa2(_build_sign_content(params), ALIPAY_PRIVATE_KEY)
    print(f"[退款] 调支付宝退款 out_trade_no={out_trade_no} refund_amount={refund_amount} out_request_no={out_request_no}", flush=True)
    resp = requests.post(ALIPAY_GATEWAY, data=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    body = data.get("alipay_trade_refund_response", {})
    if body.get("code") != "10000":
        msg = f"{body.get('sub_msg') or body.get('msg')}"
        print(f"[退款] 失败 out_trade_no={out_trade_no}: {msg}", flush=True)
        return False, f"退款失败：{msg}"
    print(f"[退款] 成功 out_trade_no={out_trade_no}", flush=True)
    return True, ""


def verify_callback_signature(provider, request_data):
    """校验第三方支付回调签名。返回 True/False。"""
    if provider == "alipay":
        sign = request_data.get("sign", "")
        if not sign:
            print("[验签] 缺少 sign", flush=True)
            return False
        params = {k: v for k, v in request_data.items()
                  if k not in ("sign", "sign_type")}
        ok = _verify_rsa2(_build_sign_content(params), sign, ALIPAY_PUBLIC_KEY)
        if not ok:
            print(f"[验签] 失败 sign前20={sign[:20]}...", flush=True)
        return ok
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
