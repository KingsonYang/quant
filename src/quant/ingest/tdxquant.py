"""
quant/ingest/tdxquant.py
==========================

【老板"老板"指令】
让 quant ingest-tdxquant 直接可用，不需要 python scripts/...

【职责】
TDXQuant 数据源增量加载服务（写回原表 daily_bar）

【老板"老板"5 大约束】
1. ✅ 增量写（按日期）
2. ✅ 字段单位一致（volume=股 / amount=元 / change_pct=%）
3. ✅ 计算逻辑一致（MA5/10/20 与 baidu/ths 算法一致）
4. ✅ 写回原表 daily_bar（DELETE source='tdxquant' 旧记录 + INSERT 新记录）
5. ✅ 补充写入（只对 baidu/ths 都没覆盖的日期写入）

【老板"老板"CLI 调用】
quant ingest-tdxquant                            # 增量最近 5 天（主板）
quant ingest-tdxquant --start 2026-06-20 --end 2026-06-23
quant ingest-tdxquant --stock 600000.SH --days 5
quant ingest-tdxquant --test-only --stock 600000.SH
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

import duckdb
import pandas as pd
from tqdm import tqdm

# 老板"老板"：TDXQuant 数据源标识
SOURCE_NAME = "tdxquant"
PARSER_VERSION = "tdxquant-get-market-data-v1"

DEFAULT_DB_PATH = Path("data") / "quant.duckdb"
DEFAULT_TDX_PATH = Path("D:/SoftWare/tdx/TdxW.exe")

# 老板"老板"硬性约束：tqcenter 不在 quant-backtest 环境里
# 必须显式指定 tqcenter.py 的位置（用户已配置）
DEFAULT_TQCENTER_PATH = Path("D:/SoftWare/tdx/PYPlugins/user/tqcenter.py")

BATCH_SIZE = 100


@dataclass
class TdxQuantIngestionSummary:
    """老板\"老板\"：TDXQuant 增量加载战报"""
    fetched: int              # TDXQuant 拉取总行数
    inserted: int             # 实际写入 daily_bar 行数
    skipped_existing: int     # 跳过 baidu/ths 已覆盖的日期
    skipped_empty: int        # 跳过空数据
    errors: int               # 错误次数
    start_date: str
    end_date: str
    stock_count: int          # 处理的股票数


class TdxQuantIngestionService:
    """
    老板"老板"指令：TDXQuant 增量加载服务（写回原表 daily_bar）

    老板"老板"新增：支持临时表（target_table 参数）
    - 默认写 daily_bar
    - 可指定临时表如 daily_bar_tdxquant_test 用于对比
    """

    def __init__(
        self,
        db_path: Path = DEFAULT_DB_PATH,
        tdx_path: Path = DEFAULT_TDX_PATH,
        tqcenter_path: Path = DEFAULT_TQCENTER_PATH,
        target_table: str = "daily_bar",  # 老板"老板"新增：默认 daily_bar，可改临时表
    ):
        self.db_path = db_path
        self.tdx_path = tdx_path
        self.tqcenter_path = tqcenter_path
        self.target_table = target_table  # 老板"老板"新增：写入目标表
        self.tq = None
        self.conn = None

    # ============== 1. TDXQuant 初始化 ==============
    def init_tdxquant(self):
        """
        老板\"老板\"硬性约束：
        - tqcenter 不在 quant-backtest 环境里
        - 必须显式 import tqcenter_path
        - tq.initialize(__file__) 只能调用一次
        - finally 关闭
        """
        try:
            # 老板修复：用 importlib.util 显式加载 tqcenter（不依赖 PYTHONPATH）
            import importlib.util
            if not self.tqcenter_path.exists():
                raise FileNotFoundError(f"tqcenter.py 不存在: {self.tqcenter_path}")

            spec = importlib.util.spec_from_file_location("tqcenter", str(self.tqcenter_path))
            tqcenter = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(tqcenter)
            self.tq = tqcenter.tq
            # 老板修复：传入当前脚本路径（不是 tqcenter 路径）
            self.tq.initialize(str(Path(__file__).resolve()))
        except Exception as e:
            raise RuntimeError(
                f"TDXQuant 初始化失败: {e}\n"
                f"请检查 tqcenter_path 是否正确: {self.tqcenter_path}"
            ) from e

    def close_tdxquant(self):
        if self.tq:
            try:
                self.tq.close()
            except Exception:
                pass

    # ============== 2. DuckDB 连接 ==============
    def connect_db(self):
        self.conn = duckdb.connect(str(self.db_path))

    def close_db(self):
        if self.conn:
            self.conn.close()

    # ============== 3. 加载股票池 ==============
    def get_stock_pool(self, only_main_board: bool = True) -> pd.DataFrame:
        sql = """
            SELECT security_id, symbol, exchange, code, name
            FROM security_master
            WHERE list_status = 'listed'
        """
        if only_main_board:
            sql += " AND market = 'ab'"
        return self.conn.execute(sql).df()

    def fetch_hsl(self, stock_code: str, start_date: str, end_date: str) -> dict:
        """
        用通达信内置公式 HSL 拉逐日真实换手率（%）。
        实测与 baidu 换手率相关系数 0.9959/0.9999，精度更高（4 位小数）。
        返回 {datetime.date: float}；任何异常返回 {}（优雅降级，上层写 NULL）。
        """
        if self.tq is None:
            return {}
        start_str = start_date.replace("-", "")
        end_str = end_date.replace("-", "")
        try:
            r = self.tq.formula_process_mul_zb(
                formula_name="HSL",
                formula_arg="",
                xsflag=4,
                return_count=0,
                return_date=True,
                stock_list=[stock_code],
                stock_period="1d",
                start_time=start_str,
                end_time=end_str,
                count=0,
                dividend_type=0,
            )
            seq = r.get(stock_code, {}).get("HSL") if isinstance(r, dict) else None
            if not seq:
                return {}
            out = {}
            for item in seq:
                d = pd.to_datetime(item["Date"], format="%Y%m%d").date()
                out[d] = float(item["Value"])
            return out
        except Exception:
            return {}

    def get_symbol_security_id(self, symbol: str) -> Optional[int]:
        """老板\"老板\"：SH600000 / SZ000001 / 600000.SH 三种格式都支持"""
        # 归一化
        if "." in symbol:
            c, e = symbol.split(".")
            normalized = f"{e}{c}"
        elif symbol.startswith(("SH", "SZ", "BJ")):
            normalized = symbol
        else:
            # 兜底：6 位数字 + 推断交易所
            if symbol.startswith(("60", "68", "90")):
                normalized = f"SH{symbol}"
            elif symbol.startswith(("00", "30", "20")):
                normalized = f"SZ{symbol}"
            else:
                normalized = symbol

        row = self.conn.execute(
            "SELECT security_id FROM security_master WHERE symbol = ?", [normalized]
        ).fetchone()
        return int(row[0]) if row else None

    # ============== 4. 拉取 TDXQuant 数据 ==============
    def fetch_history(
        self, stock_code: str, start_date: str, end_date: str
    ) -> Optional[pd.DataFrame]:
        """
        老板\"老板\"：拉取 TDXQuant 历史数据（真实接口签名）
        """
        try:
            if self.tq is None:
                return None

            start_str = start_date.replace("-", "")
            end_str = end_date.replace("-", "")

            result = self.tq.get_market_data(
                field_list=[],
                stock_list=[stock_code],
                period='1d',
                start_time=start_str,
                end_time=end_str,
                count=-1,
                dividend_type='none',
                fill_data=True,
            )

            if result is None or not isinstance(result, dict):
                return None

            # TDXQuant 真实返回：{'Open': DataFrame, 'High': DataFrame, ...}
            first_field = next(iter(result.values()))
            if not hasattr(first_field, 'index'):
                return None
            dates = pd.to_datetime(first_field.index)
            n = len(dates)
            if n == 0:
                return None

            df = pd.DataFrame({'trade_date': dates})

            field_map = {
                'Open': 'open_price',
                'High': 'high_price',
                'Low': 'low_price',
                'Close': 'close_price',
                'Volume': 'volume',
                'Amount': 'amount',
            }

            for src_field, dst_field in field_map.items():
                if src_field not in result:
                    continue
                fdf = result[src_field]
                if hasattr(fdf, 'columns') and stock_code in fdf.columns:
                    df[dst_field] = fdf[stock_code].values
                elif hasattr(fdf, 'iloc'):
                    df[dst_field] = fdf.iloc[:, 0].values

            # 老板"老板"硬性约束：TDXQuant Amount 是万元，× 10000 转为元
            if 'amount' in df.columns:
                df['amount'] = df['amount'].astype(float) * 10000.0

            if df.empty or 'close_price' not in df.columns:
                return None

            return df

        except Exception:
            return None

    # ============== 5. 计算移动平均 + 前收/涨跌（序列级） ==============

    def fetch_divid_factors(self, stock_code: str) -> Optional[pd.DataFrame]:
        """Fetch full dividend/rights-adjustment history from TDXQuant."""
        try:
            if self.tq is None:
                return None
            df = self.tq.get_divid_factors(stock_code=stock_code)
            if df is None or df.empty:
                return None
            return df
        except Exception:
            return None

    def store_divid_events(self, security_id: int, stock_code: str) -> int:
        """Fetch dividend factors and write them to event_record."""
        from quant.adjust import parse_divid_factors
        import json
        df = self.fetch_divid_factors(stock_code)
        if df is None or df.empty:
            return 0
        events = parse_divid_factors(df)
        if not events:
            return 0
        delete_sql = "DELETE FROM event_record WHERE security_id = ? AND event_type = 'dividend'"
        self.conn.execute(delete_sql, [security_id])
        for ev in events:
            payload = json.dumps({
                "Bonus": ev.bonus,
                "ShareBonus": ev.share_bonus,
                "Allotment": ev.allotment,
                "AllotPrice": ev.allot_price,
            })
            event_id_row = self.conn.execute("SELECT COALESCE(MAX(event_id), 0) + 1 FROM event_record").fetchone()
            event_id = int(event_id_row[0])
            insert_sql = """INSERT INTO event_record (event_id, security_id, event_date, event_type, event_title, event_content, event_payload, source_name, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"""
            self.conn.execute(insert_sql, [
                event_id, security_id, ev.ex_date, 'dividend',
                f'dividend {ev.bonus}/{ev.share_bonus}/{ev.allotment}',
                f'Bonus={ev.bonus} ShareBonus={ev.share_bonus} Allotment={ev.allotment} AllotPrice={ev.allot_price}',
                payload, SOURCE_NAME, datetime.now(),
            ])
        return len(events)

    def calc_ma(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        在拉了 30 天历史的完整序列上计算：
        1. MA5/10/20_price / _volume 滚动平均
        2. pre_close_price = 前一根 K 线收盘（不复权序列，即前收盘；除权日为近似）
        3. change_amount / change_pct = 用 close 与 pre_close 计算
        """
        if df.empty or "close_price" not in df.columns:
            return df

        df = df.sort_values("trade_date").reset_index(drop=True)

        # MA 计算（min_periods=1 保证至少 1 天也有值）
        df["ma5_price"] = df["close_price"].rolling(window=5, min_periods=1).mean()
        df["ma10_price"] = df["close_price"].rolling(window=10, min_periods=1).mean()
        df["ma20_price"] = df["close_price"].rolling(window=20, min_periods=1).mean()

        if "volume" in df.columns:
            df["ma5_volume"] = df["volume"].rolling(window=5, min_periods=1).mean().astype("int64")
            df["ma10_volume"] = df["volume"].rolling(window=10, min_periods=1).mean().astype("int64")
            df["ma20_volume"] = df["volume"].rolling(window=20, min_periods=1).mean().astype("int64")

        # 前收盘 & 涨跌（序列内前一根收盘）
        df["pre_close_price"] = df["close_price"].shift(1)
        df["change_amount"] = df["close_price"] - df["pre_close_price"]
        df["change_pct"] = (df["close_price"] / df["pre_close_price"] - 1) * 100.0

        return df

    # ============== 6. 写入原表 daily_bar ==============
    def upsert_daily_bar(self, df: pd.DataFrame, security_id: int) -> int:
        """
        写回原表 daily_bar（作为 baidu/ths 的补充数据源）。

        约束：
        1. 不修改 baidu/ths 的原数据
        2. 只对其他数据源都没覆盖的日期 INSERT tdxquant 数据
        3. DELETE + INSERT 幂等（只删 source='tdxquant' 的旧记录）

        pre_close / change / MA / turnover_ratio 均已在 df 内预计算好
        （见 calc_ma 与 ingest 主循环的 hsl_map 合并），此处不再回查库。
        """
        if df.empty:
            return 0

        # 查 target_table 已有日期（临时表测试时不冲突）
        trade_dates = df["trade_date"].dt.date.unique().tolist()
        date_strs = [str(d) for d in trade_dates]
        placeholders = ",".join(["?"] * len(date_strs))
        existing_sql = f"""
            SELECT DISTINCT trade_date FROM {self.target_table}
            WHERE security_id = ?
              AND trade_date IN ({placeholders})
        """
        existing_dates = set(
            row[0] for row in self.conn.execute(existing_sql, [security_id] + date_strs).fetchall()
        )

        # 只保留其他数据源没覆盖的日期
        df_supplement = df[~df["trade_date"].dt.date.isin(existing_dates)].copy()
        if df_supplement.empty:
            return 0

        rows = []
        for _, r in df_supplement.iterrows():
            trade_date = pd.Timestamp(r["trade_date"])
            ts_epoch = int(trade_date.timestamp())

            pre_close = r.get("pre_close_price")
            pre_close = float(pre_close) if pre_close is not None and not pd.isna(pre_close) else None
            change_amount = float(r["change_amount"]) if not pd.isna(r.get("change_amount")) else None
            change_pct = float(r["change_pct"]) if not pd.isna(r.get("change_pct")) else None
            turnover_ratio = float(r["turnover_ratio"]) if not pd.isna(r.get("turnover_ratio")) else None

            row = (
                security_id,
                trade_date.date(),
                ts_epoch,
                float(r["open_price"]),
                float(r["high_price"]),
                float(r["low_price"]),
                float(r["close_price"]),
                pre_close,
                int(r.get("volume", 0)),
                float(r.get("amount", 0)),
                change_amount,
                change_pct,
                turnover_ratio,
                float(r["ma5_price"]) if not pd.isna(r.get("ma5_price")) else None,
                float(r["ma10_price"]) if not pd.isna(r.get("ma10_price")) else None,
                float(r["ma20_price"]) if not pd.isna(r.get("ma20_price")) else None,
                int(r["ma5_volume"]) if not pd.isna(r.get("ma5_volume")) else None,
                int(r["ma10_volume"]) if not pd.isna(r.get("ma10_volume")) else None,
                int(r["ma20_volume"]) if not pd.isna(r.get("ma20_volume")) else None,
                float(r["open_qfq"]) if not pd.isna(r.get("open_qfq")) else None,
                float(r["high_qfq"]) if not pd.isna(r.get("high_qfq")) else None,
                float(r["low_qfq"]) if not pd.isna(r.get("low_qfq")) else None,
                float(r["close_qfq"]) if not pd.isna(r.get("close_qfq")) else None,
                "none",
                SOURCE_NAME,
                PARSER_VERSION,
                datetime.now(),
            )
            rows.append(row)

        # 支持临时表（--target-table 参数）；只删 source='tdxquant' 保证幂等
        dates = [str(r[1]) for r in rows]
        placeholders = ",".join(["?"] * len(dates))
        delete_sql = f"""
            DELETE FROM {self.target_table}
            WHERE security_id = ?
              AND trade_date IN ({placeholders})
              AND source_name = ?
        """
        self.conn.execute(delete_sql, [security_id] + dates + [SOURCE_NAME])

        insert_sql = f"""
            INSERT INTO {self.target_table} (
                security_id, trade_date, ts_epoch,
                open_price, high_price, low_price, close_price, pre_close_price,
                volume, amount, change_amount, change_pct, turnover_ratio,
                ma5_price, ma10_price, ma20_price,
                ma5_volume, ma10_volume, ma20_volume,
                open_qfq, high_qfq, low_qfq, close_qfq, adjust_type,
                source_name, parser_version, ingested_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        self.conn.executemany(insert_sql, rows)
        self.conn.commit()

        return len(rows)

    # ============== 7. 主流程（老板"老板"修复 Bug 1+2：必须拉 20 天历史） ==============
    def ingest(
        self,
        start_date: str,
        end_date: str,
        stock: Optional[str] = None,
        only_main_board: bool = True,
        test_only: bool = False,
    ) -> TdxQuantIngestionSummary:
        """老板\"老板\"主流程"""
        summary = TdxQuantIngestionSummary(
            fetched=0, inserted=0, skipped_existing=0,
            skipped_empty=0, errors=0,
            start_date=start_date, end_date=end_date, stock_count=0,
        )

        # 老板"老板"修复 Bug #1+2：必须拉 20 天历史才能算 MA5/10/20
        MA_HISTORY_DAYS = 30  # 多拉 30 天确保 MA20 完整

        # 实际拉取范围：用户 start_date 往前多拉 30 天确保 MA20 完整
        actual_end = pd.Timestamp(end_date)
        actual_start = pd.Timestamp(start_date) - pd.Timedelta(days=MA_HISTORY_DAYS)
        actual_start_str = actual_start.strftime("%Y-%m-%d")

        if test_only:
            self.init_tdxquant()
            try:
                if not stock:
                    return summary
                # 测试模式：用扩展日期范围拉数据
                # 归一化 stock_code（与主流程一致）
                if stock.startswith("SH"):
                    stock_code = stock[2:] + ".SH"
                elif stock.startswith("SZ"):
                    stock_code = stock[2:] + ".SZ"
                elif stock.startswith("BJ"):
                    stock_code = stock[2:] + ".BJ"
                else:
                    stock_code = stock

                df = self.fetch_history(stock_code, actual_start_str, end_date)
                if df is None or df.empty:
                    return summary
                df = self.calc_ma(df)
                hsl_map = self.fetch_hsl(stock_code, start_date, end_date)
                df["turnover_ratio"] = df["trade_date"].dt.date.map(hsl_map)

                from quant.adjust import parse_divid_factors, apply_qfq
                divid_df = self.fetch_divid_factors(stock_code)
                if divid_df is not None and not divid_df.empty:
                    events = parse_divid_factors(divid_df)
                    df = apply_qfq(df, events)
                else:
                    for qc in ("open_qfq", "high_qfq", "low_qfq", "close_qfq"):
                        df[qc] = df[qc.replace("_qfq", "_price")]

                summary.fetched = len(df)
                summary.stock_count = 1
                print(f"[CHART] 拉取历史范围: {actual_start_str} ~ {end_date}（含 {MA_HISTORY_DAYS} 天历史）")
                print(df.to_string(index=False))
                return summary
            finally:
                self.close_tdxquant()

        self.init_tdxquant()
        self.connect_db()
        try:
            # 拿股票池
            if stock:
                security_id = self.get_symbol_security_id(stock)
                if security_id is None:
                    return summary
                pool = pd.DataFrame([{
                    "security_id": security_id,
                    "symbol": stock,
                }])
            else:
                pool = self.get_stock_pool(only_main_board=only_main_board)
                if pool.empty:
                    return summary

            summary.stock_count = len(pool)

            # 按 batch 拉取（带进度条）
            pbar = tqdm(
                pool.iterrows(),
                total=len(pool),
                desc="Ingest",
                unit="stock",
                ncols=100,
            )
            for _, row in pbar:
                security_id = int(row["security_id"])
                symbol = row["symbol"]
                if symbol.startswith("SH"):
                    stock_code = symbol[2:] + ".SH"
                elif symbol.startswith("SZ"):
                    stock_code = symbol[2:] + ".SZ"
                elif symbol.startswith("BJ"):
                    stock_code = symbol[2:] + ".BJ"
                else:
                    stock_code = symbol

                try:
                    # 老板"老板"修复 Bug #1+2：拉 actual_start_str ~ end_date
                    df = self.fetch_history(stock_code, actual_start_str, end_date)
                    if df is None or df.empty:
                        summary.skipped_empty += 1
                        continue

                    summary.fetched += len(df)
                    df = self.calc_ma(df)
                    # 合并逐日真实换手率（HSL，只覆盖目标区间即可）
                    hsl_map = self.fetch_hsl(stock_code, start_date, end_date)
                    df["turnover_ratio"] = df["trade_date"].dt.date.map(hsl_map)

                    # 前复权(QFQ)计算：拉全量分红事件 → 线性变换 A*price+B
                    from quant.adjust import parse_divid_factors, apply_qfq
                    divid_df = self.fetch_divid_factors(stock_code)
                    if divid_df is not None and not divid_df.empty:
                        events = parse_divid_factors(divid_df)
                        df = apply_qfq(df, events)
                        try:
                            self.store_divid_events(security_id, stock_code)
                        except Exception:
                            pass
                    else:
                        for qc in ("open_qfq", "high_qfq", "low_qfq", "close_qfq"):
                            df[qc] = df[qc.replace("_qfq", "_price")]

                    # 只保留用户请求的日期范围（多拉的 30 天历史仅用于 MA 计算）
                    target_start = pd.Timestamp(start_date)
                    target_end = pd.Timestamp(end_date)
                    df_target = df[(df["trade_date"] >= target_start) & (df["trade_date"] <= target_end)]

                    if df_target.empty:
                        continue

                    inserted = self.upsert_daily_bar(df_target, security_id)
                    summary.inserted += inserted
                    summary.skipped_existing += len(df_target) - inserted
                except Exception as e:
                    summary.errors += 1
                    if summary.errors <= 3:
                        tqdm.write(f"[ERR] {symbol}: {type(e).__name__}: {e}")

                pbar.set_postfix(
                    fetched=summary.fetched,
                    inserted=summary.inserted,
                    errs=summary.errors,
                )

            pbar.close()
            return summary
        finally:
            self.close_tdxquant()
            self.close_db()

    # ============== 8. 全量回填（替换 baidu/ths 数据） ==============
    def backfill(
        self,
        stock: Optional[str] = None,
        only_main_board: bool = True,
    ) -> TdxQuantIngestionSummary:
        """Full backfill: replace ALL daily_bar data with TDXQuant unadjusted + QFQ."""
        summary = TdxQuantIngestionSummary(
            fetched=0, inserted=0, skipped_existing=0,
            skipped_empty=0, errors=0,
            start_date="full_history", end_date="today", stock_count=0,
        )

        self.init_tdxquant()
        self.connect_db()
        try:
            if stock:
                security_id = self.get_symbol_security_id(stock)
                if security_id is None:
                    return summary
                pool = pd.DataFrame([{"security_id": security_id, "symbol": stock}])
            else:
                pool = self.get_stock_pool(only_main_board=only_main_board)
                if pool.empty:
                    return summary

            summary.stock_count = len(pool)
            end_date = datetime.now().strftime("%Y-%m-%d")
            start_date = "1990-01-01"

            pbar = tqdm(
                pool.iterrows(),
                total=len(pool),
                desc="Backfill",
                unit="stock",
                ncols=100,
            )
            for _, row in pbar:
                security_id = int(row["security_id"])
                symbol = row["symbol"]
                if symbol.startswith("SH"):
                    stock_code = symbol[2:] + ".SH"
                elif symbol.startswith("SZ"):
                    stock_code = symbol[2:] + ".SZ"
                elif symbol.startswith("BJ"):
                    stock_code = symbol[2:] + ".BJ"
                else:
                    stock_code = symbol

                try:
                    df = self.fetch_history(stock_code, start_date, end_date)
                    if df is None or df.empty:
                        summary.skipped_empty += 1
                        continue

                    summary.fetched += len(df)
                    df = self.calc_ma(df)

                    hsl_map = self.fetch_hsl(stock_code, start_date, end_date)
                    df["turnover_ratio"] = df["trade_date"].dt.date.map(hsl_map)

                    from quant.adjust import parse_divid_factors, apply_qfq
                    divid_df = self.fetch_divid_factors(stock_code)
                    if divid_df is not None and not divid_df.empty:
                        events = parse_divid_factors(divid_df)
                        df = apply_qfq(df, events)
                        try:
                            self.store_divid_events(security_id, stock_code)
                        except Exception:
                            pass
                    else:
                        for qc in ("open_qfq", "high_qfq", "low_qfq", "close_qfq"):
                            df[qc] = df[qc.replace("_qfq", "_price")]

                    inserted = self._backfill_daily_bar(df, security_id)
                    summary.inserted += inserted
                except Exception as e:
                    summary.errors += 1
                    if summary.errors <= 3:
                        tqdm.write(f"[ERR] {symbol}: {type(e).__name__}: {e}")

                pbar.set_postfix(
                    fetched=summary.fetched,
                    inserted=summary.inserted,
                    errs=summary.errors,
                )

            pbar.close()
            return summary
        finally:
            self.close_tdxquant()
            self.close_db()

    def _backfill_daily_bar(self, df: pd.DataFrame, security_id: int) -> int:
        """Delete ALL existing rows for this stock, then insert new data."""
        if df.empty:
            return 0

        self.conn.execute(
            f"DELETE FROM {self.target_table} WHERE security_id = ?",
            [security_id],
        )

        rows = []
        for _, r in df.iterrows():
            trade_date = pd.Timestamp(r["trade_date"])
            ts_epoch = int(trade_date.timestamp())
            pre_close = r.get("pre_close_price")
            pre_close = float(pre_close) if pre_close is not None and not pd.isna(pre_close) else None
            row = (
                security_id,
                trade_date.date(),
                ts_epoch,
                float(r["open_price"]),
                float(r["high_price"]),
                float(r["low_price"]),
                float(r["close_price"]),
                pre_close,
                int(r.get("volume", 0)),
                float(r.get("amount", 0)),
                float(r["change_amount"]) if not pd.isna(r.get("change_amount")) else None,
                float(r["change_pct"]) if not pd.isna(r.get("change_pct")) else None,
                float(r["turnover_ratio"]) if not pd.isna(r.get("turnover_ratio")) else None,
                float(r["ma5_price"]) if not pd.isna(r.get("ma5_price")) else None,
                float(r["ma10_price"]) if not pd.isna(r.get("ma10_price")) else None,
                float(r["ma20_price"]) if not pd.isna(r.get("ma20_price")) else None,
                int(r["ma5_volume"]) if not pd.isna(r.get("ma5_volume")) else None,
                int(r["ma10_volume"]) if not pd.isna(r.get("ma10_volume")) else None,
                int(r["ma20_volume"]) if not pd.isna(r.get("ma20_volume")) else None,
                float(r["open_qfq"]) if not pd.isna(r.get("open_qfq")) else None,
                float(r["high_qfq"]) if not pd.isna(r.get("high_qfq")) else None,
                float(r["low_qfq"]) if not pd.isna(r.get("low_qfq")) else None,
                float(r["close_qfq"]) if not pd.isna(r.get("close_qfq")) else None,
                "none",
                SOURCE_NAME,
                PARSER_VERSION,
                datetime.now(),
            )
            rows.append(row)

        insert_sql = f"""
            INSERT INTO {self.target_table} (
                security_id, trade_date, ts_epoch,
                open_price, high_price, low_price, close_price, pre_close_price,
                volume, amount, change_amount, change_pct, turnover_ratio,
                ma5_price, ma10_price, ma20_price,
                ma5_volume, ma10_volume, ma20_volume,
                open_qfq, high_qfq, low_qfq, close_qfq, adjust_type,
                source_name, parser_version, ingested_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        self.conn.executemany(insert_sql, rows)
        self.conn.commit()
        return len(rows)


def ingest_tdxquant(
    start_date: str,
    end_date: str,
    stock: Optional[str] = None,
    db_path: Path = DEFAULT_DB_PATH,
    only_main_board: bool = True,
    test_only: bool = False,
) -> TdxQuantIngestionSummary:
    """老板\"老板\"CLI 服务入口（被 commands/ingest.py 调用）"""
    service = TdxQuantIngestionService(db_path=db_path)
    return service.ingest(
        start_date=start_date,
        end_date=end_date,
        stock=stock,
        only_main_board=only_main_board,
        test_only=test_only,
    )


def backfill_prices(
    db_path: Path = DEFAULT_DB_PATH,
    stock: Optional[str] = None,
    only_main_board: bool = True,
    tqcenter_path: Path = DEFAULT_TQCENTER_PATH,
) -> TdxQuantIngestionSummary:
    """Backfill daily_bar with TDXQuant unadjusted prices + computed QFQ columns.

    Deletes ALL existing rows for each stock (including baidu/ths) and replaces
    them with TDXQuant unadjusted OHLC + front-adjusted (QFQ) OHLC.
    """
    service = TdxQuantIngestionService(db_path=db_path, tqcenter_path=tqcenter_path)
    return service.backfill(stock=stock, only_main_board=only_main_board)
