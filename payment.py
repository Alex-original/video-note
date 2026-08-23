"""支付与充值（订单 + 幂等回调）。

当前为地基：下单、幂等充值已实现；第三方（微信/支付宝）验签待商户号下发后接入。

接入第三方时的改动点：
    1. create_order 里根据 provider 调用对应平台「统一下单/预下单」拿到 pay_url。
    2. 新增一个 Web 回调路由，先 verify_callback_signature 验签，再调 mark_paid。
"""
import time
import uuid

import db

ORDER_EXPIRE_SECONDS = 2 * 3600  # 订单 2 小时未支付即作废


def _gen_out_trade_no():
    return f"VN{int(time.time() * 1000)}{uuid.uuid4().hex[:6].upper()}"


def create_order(user_id, amount):
    """创建充值订单，返回 (order_id, out_trade_no)。"""
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

    now = time.time()
    session = db.get_session()
    try:
        order = db.Order(user_id=user_id, amount=amount, status="pending",
                         provider="", out_trade_no=_gen_out_trade_no(),
                         transaction_id="", pay_url="",
                         created_at=now, paid_at=None,
                         expire_at=now + ORDER_EXPIRE_SECONDS)
        session.add(order)
        session.commit()
        return order.id, order.out_trade_no
    finally:
        session.close()


def mark_paid(out_trade_no, transaction_id, provider="manual"):
    """支付回调确认：幂等充值。重复回调不重复到账，返回是否本次实际充值。"""
    session = db.get_session()
    try:
        order = (session.query(db.Order)
                 .filter(db.Order.out_trade_no == out_trade_no).first())
        if not order:
            return False
        if order.status == "paid":
            return False  # 幂等：已支付过，不重复充值
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
    """校验第三方支付回调签名。

    待商户号下发后按对应平台规则实现：
      - 支付宝：RSA2 验签（支付宝公钥）
      - 微信支付：APIv3 验签（平台证书 + 签名值）
    当前未接入，调用直接抛异常，防止把未验签的回调误判为已支付。
    """
    raise NotImplementedError(
        f"{provider} 回调验签尚未接入，需商户号与证书下发后实现"
    )
