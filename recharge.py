"""管理员充值脚本（内测期人工充值）。

用法:
    python recharge.py <手机号> <金额>
    例: python recharge.py 13800000001 10   # 充值 10 元
"""
import sys
import time

import db


def recharge(phone, amount):
    session = db.get_session()
    try:
        user = session.query(db.User).filter(db.User.phone == phone).first()
        if not user:
            print(f"❌ 未找到用户：{phone}")
            return False
        user.balance = round(user.balance + amount, 2)
        session.add(db.Billing(user_id=user.id, amount=amount, type="recharge",
                               created_at=time.time()))
        session.commit()
        print(f"✅ 已为 {phone} 充值 ¥{amount:.2f}，当前余额 ¥{user.balance:.2f}")
        return True
    finally:
        session.close()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("用法: python recharge.py <手机号> <金额>")
        sys.exit(1)
    phone = sys.argv[1]
    try:
        amount = round(float(sys.argv[2]), 2)
    except ValueError:
        print("金额必须是数字")
        sys.exit(1)
    if amount <= 0:
        print("金额必须大于 0")
        sys.exit(1)
    recharge(phone, amount)
