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
import time
from typing import Any, Callable, List, Optional

import duckdb
import pandas as pd
from tqdm import tqdm

# 老板"老板"：TDXQuant 数据源标识
SOURCE_NAME = "tdxquant"
PARSER_VERSION = "tdxquant-get-market-data-v1"

DEFAULT_DB_PATH = Path("data") / "quant.duckdb"

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
        db_path: Path | None = None,
        tdx_path: Path | None = None,
        tqcenter_path: Path | None = None,
        target_table: str = "daily_bar",  # 老板"老板"新增：默认 daily_bar，可改临时表
    ):
        from quant.config.local_settings import resolve_tdxquant_paths

        paths = resolve_tdxquant_paths(tdx_path=tdx_path, tqcenter_path=tqcenter_path)
        self.db_path = db_path or DEFAULT_DB_PATH
        self.tdx_path = paths.tdx_path
        self.tqcenter_path = paths.tqcenter_path
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
            # 按配置路径加载通达信安装目录里的 tqcenter，不依赖 PYTHONPATH。
            import importlib.util
            from quant.config.local_settings import missing_tqcenter_message

            if self.tqcenter_path is None:
                raise FileNotFoundError(missing_tqcenter_message())
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

    def _normalize_symbol(self, symbol: str) -> str:
        """Convert security_master symbol (SH600000) to TDXQuant code (600000.SH)."""
        if symbol.startswith("SH"):
            return symbol[2:] + ".SH"
        elif symbol.startswith("SZ"):
            return symbol[2:] + ".SZ"
        elif symbol.startswith("BJ"):
            return symbol[2:] + ".BJ"
        return symbol

    def fetch_history_batch(
        self, stock_codes: list[str], start_date: str, end_date: str
    ) -> dict[str, pd.DataFrame]:
        """Fetch history for multiple stocks in one API call.
        Returns {stock_code: DataFrame}."""
        try:
            if self.tq is None or not stock_codes:
                return {}

            start_str = start_date.replace("-", "")
            end_str = end_date.replace("-", "")

            result = self.tq.get_market_data(
                field_list=[],
                stock_list=stock_codes,
                period='1d',
                start_time=start_str,
                end_time=end_str,
                count=-1,
                dividend_type='none',
                fill_data=True,
            )

            if result is None or not isinstance(result, dict):
                return {}

            first_field = next(iter(result.values()))
            if not hasattr(first_field, 'index'):
                return {}
            dates = pd.to_datetime(first_field.index)
            if len(dates) == 0:
                return {}

            field_map = {
                'Open': 'open_price',
                'High': 'high_price',
                'Low': 'low_price',
                'Close': 'close_price',
                'Volume': 'volume',
                'Amount': 'amount',
            }

            stock_dfs: dict[str, pd.DataFrame] = {}
            for sc in stock_codes:
                df = pd.DataFrame({'trade_date': dates})
                for src_field, dst_field in field_map.items():
                    if src_field not in result:
                        continue
                    fdf = result[src_field]
                    if hasattr(fdf, 'columns') and sc in fdf.columns:
                        df[dst_field] = fdf[sc].values
                    elif hasattr(fdf, 'iloc') and len(fdf.columns) == 1:
                        df[dst_field] = fdf.iloc[:, 0].values

                if 'amount' in df.columns:
                    df['amount'] = df['amount'].astype(float) * 10000.0

                if not df.empty and 'close_price' in df.columns:
                    stock_dfs[sc] = df

            return stock_dfs
        except Exception:
            return {}

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
        df = self.fetch_divid_factors(stock_code)
        if df is None or df.empty:
            return 0
        events = parse_divid_factors(df)
        return self._store_divid_events(security_id, events)

    def _store_divid_events(self, security_id: int, events: list) -> int:
        """Write pre-parsed dividend events to event_record (batch insert)."""
        import json
        if not events:
            return 0
        self.conn.execute(
            "DELETE FROM event_record WHERE security_id = ? AND event_type = 'dividend'",
            [security_id],
        )
        max_id_row = self.conn.execute(
            "SELECT COALESCE(MAX(event_id), 0) + 1 FROM event_record"
        ).fetchone()
        next_id = int(max_id_row[0])
        now = datetime.now()
        rows = []
        for i, ev in enumerate(events):
            payload = json.dumps({
                "Bonus": ev.bonus,
                "ShareBonus": ev.share_bonus,
                "Allotment": ev.allotment,
                "AllotPrice": ev.allot_price,
            })
            rows.append((
                next_id + i, security_id, ev.ex_date, 'dividend',
                f'dividend {ev.bonus}/{ev.share_bonus}/{ev.allotment}',
                f'Bonus={ev.bonus} ShareBonus={ev.share_bonus} Allotment={ev.allotment} AllotPrice={ev.allot_price}',
                payload, SOURCE_NAME, now,
            ))
        insert_sql = """INSERT INTO event_record
            (event_id, security_id, event_date, event_type, event_title, event_content, event_payload, source_name, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"""
        self.conn.executemany(insert_sql, rows)
        return len(rows)

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
            vol = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
            df["volume"] = vol
            df["ma5_volume"] = vol.rolling(window=5, min_periods=1).mean().fillna(0).astype("int64")
            df["ma10_volume"] = vol.rolling(window=10, min_periods=1).mean().fillna(0).astype("int64")
            df["ma20_volume"] = vol.rolling(window=20, min_periods=1).mean().fillna(0).astype("int64")

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
        """
        if df.empty:
            return 0

        # 查 target_table 已有日期
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

        # Delete old tdxquant rows for these dates (幂等)
        supp_dates = [str(d) for d in df_supplement["trade_date"].dt.date.unique()]
        ph2 = ",".join(["?"] * len(supp_dates))
        self.conn.execute(
            f"DELETE FROM {self.target_table} WHERE security_id = ? AND trade_date IN ({ph2}) AND source_name = ?",
            [security_id] + supp_dates + [SOURCE_NAME],
        )

        # Bulk insert via register
        insert_df = self._to_insert_df(df_supplement, security_id)
        self.conn.register("_staging_upsert", insert_df)
        self.conn.execute(f"""
            INSERT INTO {self.target_table} (
                security_id, trade_date, ts_epoch,
                open_price, high_price, low_price, close_price, pre_close_price,
                volume, amount, change_amount, change_pct, turnover_ratio,
                ma5_price, ma10_price, ma20_price,
                ma5_volume, ma10_volume, ma20_volume,
                open_qfq, high_qfq, low_qfq, close_qfq, adjust_type,
                source_name, parser_version, ingested_at
            )
            SELECT * FROM _staging_upsert
        """)
        self.conn.unregister("_staging_upsert")
        # No commit here — caller commits per batch
        return len(insert_df)

    # ============== 7. 主流程（老板"老板"修复 Bug 1+2：必须拉 20 天历史） ==============
    def ingest(
        self,
        start_date: str,
        end_date: str,
        stock: Optional[str] = None,
        only_main_board: bool = True,
        test_only: bool = False,
        skip_hsl: bool = False,
        batch_size: int = 50,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> TdxQuantIngestionSummary:
        """Incremental ingest from TDXQuant; supplements baidu/ths in daily_bar."""
        summary = TdxQuantIngestionSummary(
            fetched=0, inserted=0, skipped_existing=0,
            skipped_empty=0, errors=0,
            start_date=start_date, end_date=end_date, stock_count=0,
        )

        MA_HISTORY_DAYS = 30
        actual_end = pd.Timestamp(end_date)
        actual_start = pd.Timestamp(start_date) - pd.Timedelta(days=MA_HISTORY_DAYS)
        actual_start_str = actual_start.strftime("%Y-%m-%d")

        if test_only:
            self.init_tdxquant()
            try:
                if not stock:
                    return summary
                stock_code = self._normalize_symbol(stock)
                df = self.fetch_history(stock_code, actual_start_str, end_date)
                if df is None or df.empty:
                    return summary
                df = self.calc_ma(df)
                if not skip_hsl:
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
                print(f"[CHART] range: {actual_start_str} ~ {end_date}")
                print(df.to_string(index=False))
                return summary
            finally:
                self.close_tdxquant()

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
            pool = pool.copy()
            pool["stock_code"] = pool["symbol"].apply(self._normalize_symbol)

            from quant.adjust import parse_divid_factors, apply_qfq

            pbar = tqdm(total=len(pool), desc="Ingest", unit="stock", ncols=100)
            total_batches = (len(pool) + batch_size - 1) // batch_size
            last_report_at = 0.0
            total_stocks = len(pool)

            def report_progress(symbol: str | None = None) -> None:
                nonlocal last_report_at
                if progress_callback is None:
                    return
                current = int(pbar.n)
                now = time.time()
                if (
                    current not in (0, 1, total_stocks)
                    and current % 10 != 0
                    and (now - last_report_at) < 0.5
                ):
                    return
                last_report_at = now
                progress_callback(
                    {
                        "current": current,
                        "total": total_stocks,
                        "fetched": summary.fetched,
                        "inserted": summary.inserted,
                        "errors": summary.errors,
                        "symbol": symbol,
                    }
                )

            report_progress()

            for batch_idx in range(total_batches):
                si = batch_idx * batch_size
                ei = min(si + batch_size, len(pool))
                batch = pool.iloc[si:ei]
                batch_codes = batch["stock_code"].tolist()

                # Batch API call
                stock_dfs = self.fetch_history_batch(batch_codes, actual_start_str, end_date)

                for _, row in batch.iterrows():
                    security_id = int(row["security_id"])
                    stock_code = row["stock_code"]
                    symbol = row["symbol"]

                    try:
                        df = stock_dfs.get(stock_code)
                        if df is None or df.empty:
                            df = self.fetch_history(stock_code, actual_start_str, end_date)
                        if df is None or df.empty:
                            summary.skipped_empty += 1
                            pbar.update(1)
                            report_progress(symbol)
                            continue

                        # Drop NaN OHLC rows
                        df = df.dropna(subset=["open_price", "close_price"])
                        if df.empty:
                            summary.skipped_empty += 1
                            pbar.update(1)
                            report_progress(symbol)
                            continue

                        summary.fetched += len(df)
                        df = self.calc_ma(df)

                        if not skip_hsl:
                            hsl_map = self.fetch_hsl(stock_code, start_date, end_date)
                            df["turnover_ratio"] = df["trade_date"].dt.date.map(hsl_map)
                        else:
                            df["turnover_ratio"] = None

                        divid_df = self.fetch_divid_factors(stock_code)
                        if divid_df is not None and not divid_df.empty:
                            events = parse_divid_factors(divid_df)
                            df = apply_qfq(df, events)
                            try:
                                self._store_divid_events(security_id, events)
                            except Exception:
                                pass
                        else:
                            for qc in ("open_qfq", "high_qfq", "low_qfq", "close_qfq"):
                                df[qc] = df[qc.replace("_qfq", "_price")]

                        # Only keep user-requested date range
                        target_start = pd.Timestamp(start_date)
                        target_end = pd.Timestamp(end_date)
                        df_target = df[(df["trade_date"] >= target_start) & (df["trade_date"] <= target_end)]

                        if df_target.empty:
                            pbar.update(1)
                            report_progress(symbol)
                            continue

                        inserted = self.upsert_daily_bar(df_target, security_id)
                        summary.inserted += inserted
                        summary.skipped_existing += len(df_target) - inserted
                    except Exception as e:
                        summary.errors += 1
                        if summary.errors <= 5:
                            tqdm.write(f"[ERR] {symbol}: {type(e).__name__}: {e}")

                    pbar.update(1)
                    pbar.set_postfix(
                        fetched=summary.fetched,
                        inserted=summary.inserted,
                        errs=summary.errors,
                    )
                    report_progress(symbol)

                # Commit per batch
                self.conn.commit()

            pbar.close()
            report_progress()
            return summary
        finally:
            self.close_tdxquant()
            self.close_db()

    # ============== 8. 全量回填（替换 baidu/ths 数据） ==============
    def backfill(
        self,
        stock: Optional[str] = None,
        only_main_board: bool = True,
        skip_hsl: bool = True,
        batch_size: int = 50,
    ) -> TdxQuantIngestionSummary:
        """Full backfill: replace ALL daily_bar data with TDXQuant unadjusted + QFQ.

        Optimizations vs ingest():
        - Batch get_market_data (1 API call per batch_size stocks, not per stock)
        - Skip HSL (turnover_ratio) by default — can backfill separately later
        - DuckDB register() + INSERT INTO ... SELECT (columnar bulk insert)
        - Commit per batch (not per stock)
        """
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

            pool = pool.copy()
            pool["stock_code"] = pool["symbol"].apply(self._normalize_symbol)

            from quant.adjust import parse_divid_factors, apply_qfq

            pbar = tqdm(total=len(pool), desc="Backfill", unit="stock", ncols=100)
            total_batches = (len(pool) + batch_size - 1) // batch_size

            for batch_idx in range(total_batches):
                si = batch_idx * batch_size
                ei = min(si + batch_size, len(pool))
                batch = pool.iloc[si:ei]
                batch_codes = batch["stock_code"].tolist()

                # --- Batch API call: 1 get_market_data for batch_size stocks ---
                stock_dfs = self.fetch_history_batch(batch_codes, start_date, end_date)

                for _, row in batch.iterrows():
                    security_id = int(row["security_id"])
                    stock_code = row["stock_code"]
                    symbol = row["symbol"]

                    try:
                        df = stock_dfs.get(stock_code)
                        if df is None or df.empty:
                            # Fallback: try single-stock fetch if batch missed it
                            df = self.fetch_history(stock_code, start_date, end_date)
                        if df is None or df.empty:
                            summary.skipped_empty += 1
                            pbar.update(1)
                            continue

                        # Drop rows where OHLC is NaN (stock not yet listed in batch date range)
                        df = df.dropna(subset=["open_price", "close_price"])
                        if df.empty:
                            summary.skipped_empty += 1
                            pbar.update(1)
                            continue

                        summary.fetched += len(df)
                        df = self.calc_ma(df)

                        if not skip_hsl:
                            hsl_map = self.fetch_hsl(stock_code, start_date, end_date)
                            df["turnover_ratio"] = df["trade_date"].dt.date.map(hsl_map)
                        else:
                            df["turnover_ratio"] = None

                        divid_df = self.fetch_divid_factors(stock_code)
                        if divid_df is not None and not divid_df.empty:
                            events = parse_divid_factors(divid_df)
                            df = apply_qfq(df, events)
                            try:
                                self._store_divid_events(security_id, events)
                            except Exception:
                                pass
                        else:
                            for qc in ("open_qfq", "high_qfq", "low_qfq", "close_qfq"):
                                df[qc] = df[qc.replace("_qfq", "_price")]

                        inserted = self._backfill_daily_bar(df, security_id)
                        summary.inserted += inserted
                    except Exception as e:
                        summary.errors += 1
                        if summary.errors <= 5:
                            tqdm.write(f"[ERR] {symbol}: {type(e).__name__}: {e}")

                    pbar.update(1)
                    pbar.set_postfix(
                        fetched=summary.fetched,
                        inserted=summary.inserted,
                        errs=summary.errors,
                    )

                # Commit per batch (not per stock)
                self.conn.commit()

            pbar.close()
            return summary
        finally:
            self.close_tdxquant()
            self.close_db()

    def _to_insert_df(self, df: pd.DataFrame, security_id: int) -> pd.DataFrame:
        """Convert processed OHLC DataFrame to insert-ready format (27 columns)."""
        ts = pd.to_datetime(df["trade_date"])
        # Handle NaN in numeric columns before type conversion
        vol = pd.to_numeric(df.get("volume", 0), errors="coerce").fillna(0)
        amt = pd.to_numeric(df.get("amount", 0), errors="coerce").fillna(0)
        ma5v = pd.to_numeric(df.get("ma5_volume", 0), errors="coerce").fillna(0)
        ma10v = pd.to_numeric(df.get("ma10_volume", 0), errors="coerce").fillna(0)
        ma20v = pd.to_numeric(df.get("ma20_volume", 0), errors="coerce").fillna(0)

        out = pd.DataFrame({
            "security_id": security_id,
            "trade_date": ts.dt.date,
            "ts_epoch": (ts.astype("int64") // 10**9).astype("int64"),
            "open_price": pd.to_numeric(df["open_price"], errors="coerce").astype(float),
            "high_price": pd.to_numeric(df["high_price"], errors="coerce").astype(float),
            "low_price": pd.to_numeric(df["low_price"], errors="coerce").astype(float),
            "close_price": pd.to_numeric(df["close_price"], errors="coerce").astype(float),
            "pre_close_price": pd.to_numeric(df["pre_close_price"], errors="coerce").astype(float),
            "volume": vol.astype("int64"),
            "amount": amt.astype(float),
            "change_amount": pd.to_numeric(df["change_amount"], errors="coerce").astype(float),
            "change_pct": pd.to_numeric(df["change_pct"], errors="coerce").astype(float),
            "turnover_ratio": pd.to_numeric(df.get("turnover_ratio", None), errors="coerce").astype(float),
            "ma5_price": pd.to_numeric(df["ma5_price"], errors="coerce").astype(float),
            "ma10_price": pd.to_numeric(df["ma10_price"], errors="coerce").astype(float),
            "ma20_price": pd.to_numeric(df["ma20_price"], errors="coerce").astype(float),
            "ma5_volume": ma5v.astype("int64"),
            "ma10_volume": ma10v.astype("int64"),
            "ma20_volume": ma20v.astype("int64"),
            "open_qfq": pd.to_numeric(df["open_qfq"], errors="coerce").astype(float),
            "high_qfq": pd.to_numeric(df["high_qfq"], errors="coerce").astype(float),
            "low_qfq": pd.to_numeric(df["low_qfq"], errors="coerce").astype(float),
            "close_qfq": pd.to_numeric(df["close_qfq"], errors="coerce").astype(float),
            "adjust_type": "none",
            "source_name": SOURCE_NAME,
            "parser_version": PARSER_VERSION,
            "ingested_at": datetime.now(),
        })
        return out

    def _backfill_daily_bar(self, df: pd.DataFrame, security_id: int) -> int:
        """Delete ALL existing rows for this stock, then bulk insert via register."""
        if df.empty:
            return 0

        self.conn.execute(
            f"DELETE FROM {self.target_table} WHERE security_id = ?",
            [security_id],
        )

        insert_df = self._to_insert_df(df, security_id)
        self.conn.register("_staging", insert_df)
        self.conn.execute(f"""
            INSERT INTO {self.target_table} (
                security_id, trade_date, ts_epoch,
                open_price, high_price, low_price, close_price, pre_close_price,
                volume, amount, change_amount, change_pct, turnover_ratio,
                ma5_price, ma10_price, ma20_price,
                ma5_volume, ma10_volume, ma20_volume,
                open_qfq, high_qfq, low_qfq, close_qfq, adjust_type,
                source_name, parser_version, ingested_at
            )
            SELECT * FROM _staging
        """)
        self.conn.unregister("_staging")
        # No commit here — caller (backfill) commits per batch
        return len(insert_df)


def ingest_tdxquant(
    start_date: str,
    end_date: str,
    stock: Optional[str] = None,
    db_path: Path = DEFAULT_DB_PATH,
    only_main_board: bool = True,
    test_only: bool = False,
    skip_hsl: bool = False,
    batch_size: int = 50,
    tqcenter_path: Path | None = None,
    tdx_path: Path | None = None,
) -> TdxQuantIngestionSummary:
    """CLI entry point (called by commands/ingest.py)."""
    service = TdxQuantIngestionService(
        db_path=db_path,
        tqcenter_path=tqcenter_path,
        tdx_path=tdx_path,
    )
    return service.ingest(
        start_date=start_date,
        end_date=end_date,
        stock=stock,
        only_main_board=only_main_board,
        test_only=test_only,
        skip_hsl=skip_hsl,
        batch_size=batch_size,
    )


def backfill_prices(
    db_path: Path = DEFAULT_DB_PATH,
    stock: Optional[str] = None,
    only_main_board: bool = True,
    tqcenter_path: Path | None = None,
    skip_hsl: bool = True,
    batch_size: int = 50,
) -> TdxQuantIngestionSummary:
    """Backfill daily_bar with TDXQuant unadjusted prices + computed QFQ columns.

    Deletes ALL existing rows for each stock (including baidu/ths) and replaces
    them with TDXQuant unadjusted OHLC + front-adjusted (QFQ) OHLC.
    """
    service = TdxQuantIngestionService(db_path=db_path, tqcenter_path=tqcenter_path)
    return service.backfill(
        stock=stock,
        only_main_board=only_main_board,
        skip_hsl=skip_hsl,
        batch_size=batch_size,
    )
