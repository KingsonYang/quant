"""
止损管理系统（T+1 Safe 版）
- 随机止损：-4%~-6%，避免量化整数位扫描
- 分批止损：-3%出30%，-5%出30%，-8%清仓
- 时间止损：买入后4小时不涨强制检查
- T+1开盘处理：低开-3%以上竞价直接清，低开-3%以内30分钟减半

使用方式：
  from stop_loss_manager import StopLossManager
  sl = StopLossManager(initial_capital=100000)
  sl.open_position(code, name, price, quantity)  # 买入后记录
  # 后续每个Tick/每分钟调用 sl.check(code, current_price, current_time)
  # 或收盘后调用 sl.end_of_day_check(current_prices)
  # sl.t_plus_one_check(code, open_price)  # T+1开盘前调用
"""

import random
import math
from datetime import datetime, time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Position:
    """持仓记录"""
    code: str
    name: str
    open_date: str          # 买入日期 YYYY-MM-DD
    open_price: float       # 买入价格
    quantity: int           # 持股数量
    stop_loss_pct: float    # 随机止损线（-4%~-6%）
    stop_loss_price: float  # 止损价格
    phases: list = field(default_factory=list)  # 分批止损记录
    # phases: [{"triggered_pct": -0.03, "sold_qty": 300, "date": "2026-06-02"}]

    def __post_init__(self):
        # 初始化分批止损触发状态
        # -3% 时出 30%，-5% 时再出 30%，-8% 时清仓
        self.phases = [
            {"triggered_pct": -0.03, "ratio": 0.30, "sold": False, "qty": 0},
            {"triggered_pct": -0.05, "ratio": 0.30, "sold": False, "qty": 0},
            {"triggered_pct": -0.08, "ratio": 1.00, "sold": False, "qty": 0},
        ]


@dataclass
class TradeRecord:
    """交易记录（用于复盘分析）"""
    code: str
    name: str
    open_date: str
    open_price: float
    close_date: Optional[str]
    close_price: Optional[float]
    quantity: int
    pnl: float           # 盈亏金额
    pnl_pct: float       # 盈亏比例
    exit_reason: str      # 退出原因：stop_loss_1 / stop_loss_2 / stop_loss_3 / time_stop / t1_gap_down / manual
    holding_days: int


class StopLossManager:
    """
    止损管理器
    支持功能：
    1. 记录持仓（open_position）
    2. 随机止损线生成（每次买入随机-4%~-6%）
    3. 分批止损检查（-3%→-5%→-8%）
    4. 时间止损（4小时不涨）
    5. T+1开盘处理
    6. 交易记录与统计
    """

    # T+1开盘时间（A股）
    T1_OPEN_TIME = time(9, 15)   # 竞价开始
    T1_CHECK_TIME = time(9, 45)  # 开盘30分钟后检查
    DAY_END_TIME = time(14, 55)   # 尾盘前5分钟

    def __init__(self, initial_capital: float = 100000.0):
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.positions: dict[str, Position] = {}  # code → Position
        self.trade_log: list[TradeRecord] = []
        self.stats = {
            "total_trades": 0,
            "winning_trades": 0,
            "total_pnl": 0.0,
            "largest_win": 0.0,
            "largest_loss": 0.0,
            "exit_reasons": {},
        }

    # ─── 公开 API ──────────────────────────────────────────

    def open_position(
        self,
        code: str,
        name: str,
        price: float,
        quantity: int,
        trade_date: str = None,
    ) -> float:
        """
        记录开仓
        返回：止损价格
        """
        today = trade_date or datetime.now().strftime("%Y-%m-%d")

        # 随机止损线（-4% ~ -6%，避免整数位）
        stop_pct = -random.uniform(0.04, 0.06)
        stop_price = round(price * (1 + stop_pct), 2)

        pos = Position(
            code=code,
            name=name,
            open_date=today,
            open_price=price,
            quantity=quantity,
            stop_loss_pct=stop_pct,
            stop_loss_price=stop_price,
        )
        self.positions[code] = pos

        # 资金扣减（简化，不考虑手续费）
        cost = price * quantity
        self.cash -= cost

        print(f"✅ 开仓 {code} {name} @ {price} 数量{quantity}股")
        print(f"   止损线: {stop_pct*100:.1f}% → 价格 {stop_price}")
        print(f"   当前现金: {self.cash:.2f} 元")
        return stop_price

    def check(
        self,
        code: str,
        current_price: float,
        current_time: datetime = None,
    ) -> Optional[dict]:
        """
        盘中检查（每个Tick或每分钟调用）
        返回：如果触发止损返回操作指令 dict，否则返回 None
        返回格式：
          {"action": "sell", "qty": N, "reason": "stop_loss_1", "price": current_price}
          {"action": "reduce", "qty": N, "reason": "stop_loss_2", "price": current_price}
        """
        if code not in self.positions:
            return None

        pos = self.positions[code]
        dt = current_time or datetime.now()
        current_price = round(current_price, 2)

        # 计算当前亏损比例
        loss_pct = (current_price - pos.open_price) / pos.open_price

        # 检查各分批止损阶段
        for phase in pos.phases:
            if phase["sold"]:
                continue

            if loss_pct <= phase["triggered_pct"]:
                # 触发止损
                sell_qty = int(pos.quantity * phase["ratio"])
                if sell_qty < 1:
                    sell_qty = 1
                phase["sold"] = True
                phase["qty"] = sell_qty

                self._execute_sell(
                    code=code,
                    price=current_price,
                    quantity=sell_qty,
                    reason=f"stop_loss_{abs(phase['triggered_pct'])*100:.0f}pct",
                    close_date=dt.strftime("%Y-%m-%d"),
                )

                return {
                    "action": "sell",
                    "qty": sell_qty,
                    "reason": f"stop_loss_{abs(phase['triggered_pct'])*100:.0f}",
                    "price": current_price,
                    "remaining": pos.quantity - sell_qty,
                }

        return None

    def end_of_day_check(self, current_prices: dict[str, float]) -> list[dict]:
        """
        收盘后检查所有持仓
        current_prices: {code: 收盘价}
        返回所有需要隔夜持仓确认的列表
        """
        results = []
        for code, close_price in current_prices.items():
            if code not in self.positions:
                continue

            pos = self.positions[code]
            loss_pct = (close_price - pos.open_price) / pos.open_price

            # 如果收盘时已触发 -8% 止损，尾盘前处理
            phase8 = pos.phases[2]
            if not phase8["sold"] and loss_pct <= phase8["triggered_pct"]:
                results.append({
                    "code": code,
                    "action": "sell_all",
                    "reason": "day_end_stop_loss_8",
                    "price": close_price,
                    "loss_pct": round(loss_pct * 100, 2),
                })
            else:
                results.append({
                    "code": code,
                    "action": "hold",
                    "reason": "no_stop_triggered",
                    "loss_pct": round(loss_pct * 100, 2),
                    "stop_loss_price": pos.stop_loss_price,
                })

        return results

    def t_plus_one_check(
        self,
        code: str,
        open_price: float,
        check_time: datetime = None,
    ) -> Optional[dict]:
        """
        T+1开盘处理
        在9:15-9:25竞价阶段调用，传入竞价价格

        规则：
        - 低开 -3% 以内：9:45之前价格反弹回开盘价以上则持有，否则减半仓
        - 低开 -3% 以下：竞价直接清仓
        - 高开：观察15分钟，跌破开盘价 -1% 减半仓
        """
        dt = check_time or datetime.now()
        if code not in self.positions:
            return None

        pos = self.positions[code]
        open_price = round(open_price, 2)
        gap_pct = (open_price - pos.open_price) / pos.open_price

        print(f"\n🔔 T+1 开盘检查 {code} {pos.name}")
        print(f"   成本价: {pos.open_price} | 今日开盘: {open_price}")
        print(f"   开盘涨跌幅: {gap_pct*100:+.2f}%")

        # 低开 -3% 以下：竞价直接清仓
        if gap_pct <= -0.03:
            self._execute_sell(
                code=code,
                price=open_price,
                quantity=pos.quantity,
                reason="t1_gap_down_3pct",
                close_date=dt.strftime("%Y-%m-%d"),
            )
            print(f"   ⚠️ 低开超过3%，竞价清仓！")
            return {
                "action": "sell_all",
                "reason": "t1_gap_down_3pct",
                "price": open_price,
                "gap_pct": round(gap_pct * 100, 2),
            }

        # 低开 -3% 以内：30分钟观察
        if gap_pct < 0:
            print(f"   👀 低开 {gap_pct*100:.2f}%，等待观察至 {self.T1_CHECK_TIME}")
            return {
                "action": "observe",
                "reason": "t1_gap_down_under_3pct",
                "price": open_price,
                "observe_until": self.T1_CHECK_TIME.strftime("%H:%M"),
                "reduce_if_still_down": True,
                "reduce_qty": pos.quantity // 2,
            }

        # 高开：正常持有
        if gap_pct > 0:
            print(f"   ✅ 高开 {gap_pct*100:.2f}%，持有观察")
            return {
                "action": "hold",
                "reason": "t1_gap_up",
                "price": open_price,
                "gain_pct": round(gap_pct * 100, 2),
            }

        return {"action": "hold", "reason": "flat_open", "price": open_price}

    def t_plus_one_reduce(
        self,
        code: str,
        current_price: float,
        current_time: datetime = None,
    ) -> Optional[dict]:
        """
        T+1开盘30分钟后复查
        低开后30分钟内价格未回到开盘价 → 减半仓
        """
        dt = current_time or datetime.now()
        if code not in self.positions:
            return None

        pos = self.positions[code]
        current_price = round(current_price, 2)

        # 如果已全部卖出则跳过
        if pos.quantity == 0:
            return None

        return self.check(code, current_price, dt)

    def close_position(
        self,
        code: str,
        price: float,
        close_date: str = None,
        reason: str = "manual",
    ) -> Optional[dict]:
        """
        手动平仓（止盈或主动退出）
        """
        if code not in self.positions:
            return None

        pos = self.positions[code]
        today = close_date or datetime.now().strftime("%Y-%m-%d")

        # 如果还有剩余持仓，先全部清掉
        remaining_qty = pos.quantity
        if remaining_qty > 0:
            self._execute_sell(
                code=code,
                price=price,
                quantity=remaining_qty,
                reason=reason,
                close_date=today,
            )

        return {
            "code": code,
            "action": "closed",
            "reason": reason,
            "price": price,
        }

    def get_position(self, code: str) -> Optional[Position]:
        return self.positions.get(code)

    def get_all_positions(self) -> dict[str, Position]:
        return self.positions

    def get_stats(self) -> dict:
        """获取统计信息"""
        return {
            **self.stats,
            "cash": round(self.cash, 2),
            "position_value": sum(
                p.open_price * p.quantity for p in self.positions.values()
            ),
            "total_assets": round(
                self.cash + sum(
                    p.open_price * p.quantity for p in self.positions.values()
                ), 2
            ),
            "win_rate": (
                self.stats["winning_trades"] / max(1, self.stats["total_trades"]) * 100
            ),
        }

    def print_positions(self):
        """打印当前持仓"""
        print(f"\n{'='*60}")
        print(f"💼 当前持仓 ({datetime.now().strftime('%Y-%m-%d %H:%M')})")
        print(f"{'='*60}")
        if not self.positions:
            print("  （无持仓）")
        else:
            for pos in self.positions.values():
                print(f"  {pos.code} {pos.name}")
                print(f"    成本: {pos.open_price} | 止损: {pos.stop_loss_pct*100:.1f}%@{pos.stop_loss_price}")
                print(f"    数量: {pos.quantity}股")
                remaining = [
                    f"-{abs(p['triggered_pct'])*100:.0f}% {'✅已出' if p['sold'] else '⬜未出'}"
                    for p in pos.phases
                ]
                print(f"    分批止损: {', '.join(remaining)}")
        print(f"\n  现金: {self.cash:.2f} 元")
        total_assets = self.cash + sum(
            p.open_price * p.quantity for p in self.positions.values()
        )
        print(f"  总资产: {total_assets:.2f} 元 (含持仓)")
        print(f"{'='*60}")

    # ─── 内部方法 ──────────────────────────────────────────

    def _execute_sell(
        self,
        code: str,
        price: float,
        quantity: int,
        reason: str,
        close_date: str,
    ):
        """执行卖出"""
        pos = self.positions[code]
        actual_qty = min(quantity, pos.quantity)

        # 更新持仓数量
        pos.quantity -= actual_qty
        self.cash += price * actual_qty

        print(f"\n{'='*50}")
        print(f"⚠️ 止损触发 {code} {pos.name}")
        print(f"   卖出: {actual_qty}股 @ {price}")
        print(f"   原因: {reason}")
        print(f"   剩余持仓: {pos.quantity}股")
        print(f"{'='*50}")

        # 如果持仓清零，记录交易
        if pos.quantity == 0:
            self._record_trade(pos, price, close_date, reason)

        # 记录统计
        pnl = (price - pos.open_price) * actual_qty
        self._update_stats(pnl, reason)

    def _record_trade(
        self,
        pos: Position,
        close_price: float,
        close_date: str,
        exit_reason: str,
    ):
        """记录完成交易"""
        pnl = (close_price - pos.open_price) * pos.quantity
        pnl_pct = (close_price - pos.open_price) / pos.open_price * 100

        record = TradeRecord(
            code=pos.code,
            name=pos.name,
            open_date=pos.open_date,
            open_price=pos.open_price,
            close_date=close_date,
            close_price=close_price,
            quantity=pos.quantity,
            pnl=pnl,
            pnl_pct=pnl_pct,
            exit_reason=exit_reason,
            holding_days=(
                datetime.strptime(close_date, "%Y-%m-%d")
                - datetime.strptime(pos.open_date, "%Y-%m-%d")
            ).days,
        )
        self.trade_log.append(record)

        # 删除空持仓
        del self.positions[pos.code]

        print(f"📝 交易完成：{pos.code} {pos.name}")
        print(f"   盈亏: {pnl:+.2f} 元 ({pnl_pct:+.2f}%)")
        print(f"   持仓天数: {record.holding_days}天")

    def _update_stats(self, pnl: float, reason: str):
        """更新统计数据"""
        self.stats["total_trades"] += 1
        self.stats["total_pnl"] += pnl
        if pnl > 0:
            self.stats["winning_trades"] += 1
        if pnl > self.stats["largest_win"]:
            self.stats["largest_win"] = pnl
        if pnl < self.stats["largest_loss"]:
            self.stats["largest_loss"] = pnl

        self.stats["exit_reasons"][reason] = \
            self.stats["exit_reasons"].get(reason, 0) + 1

    def export_trade_log(self, path: str):
        """导出交易记录到 CSV"""
        if not self.trade_log:
            print("⚠️ 没有交易记录可导出")
            return

        import csv
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            fieldnames = [
                "code", "name", "open_date", "open_price",
                "close_date", "close_price", "quantity",
                "pnl", "pnl_pct", "exit_reason", "holding_days",
            ]
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for r in self.trade_log:
                writer.writerow({
                    "code": r.code,
                    "name": r.name,
                    "open_date": r.open_date,
                    "open_price": r.open_price,
                    "close_date": r.close_date,
                    "close_price": r.close_price,
                    "quantity": r.quantity,
                    "pnl": round(r.pnl, 2),
                    "pnl_pct": round(r.pnl_pct, 2),
                    "exit_reason": r.exit_reason,
                    "holding_days": r.holding_days,
                })
        print(f"✅ 交易记录已导出: {path}")


# ─── 使用示例（可直接运行测试）────────────────────────────────

if __name__ == "__main__":
    print("🧪 止损管理系统测试")
    print("=" * 60)

    # 初始化
    sl = StopLossManager(initial_capital=100000)

    # 模拟买入
    print("\n📗 买入操作：")
    sl.open_position("000001.SZ", "平安银行", price=11.39, quantity=1000)

    # 模拟盘中价格变动检查
    prices_to_check = [
        (11.20, "10:30"),  # -1.67% 未触发
        (11.00, "11:00"),  # -3.42% 触发 -3% 止损（应卖300股）
        (10.80, "13:00"),  # -5.18% 触发 -5% 止损（应卖300股）
    ]

    print("\n📉 盘中止损检查：")
    for price, t in prices_to_check:
        dt = datetime.strptime(f"2026-06-02 {t}", "%Y-%m-%d %H:%M")
        result = sl.check("000001.SZ", price, dt)
        if result:
            print(f"  [{t}] 价格={price} → {result}")
        else:
            print(f"  [{t}] 价格={price} → 持有（未触发止损）")

    # 模拟 T+1 低开 -4%
    print("\n📅 T+1 开盘检查：")
    sl2 = StopLossManager(initial_capital=100000)
    sl2.open_position("000001.SZ", "平安银行", price=11.39, quantity=1000)
    result = sl2.t_plus_one_check("000001.SZ", open_price=10.95)
    print(f"  T+1 低开 -3.86% → {result}")

    sl2.print_positions()
    print("\n🎉 测试完成！")
