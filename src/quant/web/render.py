from __future__ import annotations

from html import escape
from pathlib import Path
from string import Template
from typing import Any

from quant.web.service import DEFAULT_SELECTION_FORM, DEFAULT_STRATEGY_FORM, ServiceResult


BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"


def load_template(name: str) -> Template:
    return Template((TEMPLATES_DIR / name).read_text(encoding="utf-8"))


def _nav(current_page: str) -> str:
    links = [
        ("selection", "/backtests/selection", "普通回测"),
        ("strategy", "/backtests/strategy", "策略回测"),
    ]
    items = []
    for page, href, label in links:
        class_name = "nav-link is-active" if current_page == page else "nav-link"
        items.append(f'<a class="{class_name}" href="{href}">{escape(label)}</a>')
    return "".join(items)


def _error_list(items: list[str]) -> str:
    if not items:
        return ""
    rows = "".join(f"<li>{escape(item)}</li>" for item in items)
    return f'<div class="alert alert-error"><strong>执行失败</strong><ul>{rows}</ul></div>'


def _field_error(field_errors: dict[str, str], field_name: str) -> str:
    message = field_errors.get(field_name)
    if not message:
        return ""
    return f'<p class="field-error">{escape(message)}</p>'


def _text_input(
    label: str,
    name: str,
    value: str,
    field_errors: dict[str, str],
    input_type: str = "text",
) -> str:
    return (
        f'<label class="field"><span>{escape(label)}</span>'
        f'<input type="{input_type}" name="{escape(name)}" value="{escape(value)}"></label>'
        f"{_field_error(field_errors, name)}"
    )


def _textarea(
    label: str, name: str, value: str, field_errors: dict[str, str], rows: int = 8
) -> str:
    return (
        f'<label class="field field-wide"><span>{escape(label)}</span>'
        f'<textarea name="{escape(name)}" rows="{rows}">{escape(value)}</textarea></label>'
        f"{_field_error(field_errors, name)}"
    )


def _select(
    label: str,
    name: str,
    value: str,
    options: list[tuple[str, str]],
    field_errors: dict[str, str],
) -> str:
    option_html = []
    for option_value, option_label in options:
        selected = ' selected="selected"' if option_value == value else ""
        option_html.append(
            f'<option value="{escape(option_value)}"{selected}>{escape(option_label)}</option>'
        )
    return (
        f'<label class="field"><span>{escape(label)}</span>'
        f'<select name="{escape(name)}">{"".join(option_html)}</select></label>'
        f"{_field_error(field_errors, name)}"
    )


def _summary_cards(items: list[tuple[str, Any]]) -> str:
    cards = []
    for label, value in items:
        display_value = "-" if value in (None, "") else str(value)
        cards.append(
            f'<div class="stat-card"><span class="stat-label">{escape(label)}</span>'
            f'<strong class="stat-value">{escape(display_value)}</strong></div>'
        )
    return f'<section class="stats-grid">{"".join(cards)}</section>'


def _table(columns: list[tuple[str, str]], rows: list[dict[str, Any]], empty_text: str) -> str:
    if not rows:
        return f'<div class="empty-state">{escape(empty_text)}</div>'
    header_html = "".join(f"<th>{escape(label)}</th>" for _, label in columns)
    body_rows = []
    for row in rows:
        cells = "".join(
            f"<td>{escape(str(row.get(key, '-') if row.get(key, '-') is not None else '-'))}</td>"
            for key, _ in columns
        )
        body_rows.append(f"<tr>{cells}</tr>")
    return (
        '<div class="table-scroll"><table><thead><tr>'
        f"{header_html}</tr></thead><tbody>{''.join(body_rows)}</tbody></table></div>"
    )


def _distribution_table(distribution: list[dict[str, Any]]) -> str:
    if not distribution:
        return '<div class="empty-state">暂无收益分布数据。</div>'
    return _table([("label", "区间"), ("count", "数量")], distribution, "暂无收益分布数据。")


def _selection_form(result: ServiceResult) -> str:
    values = DEFAULT_SELECTION_FORM | result.form_values
    fields = [
        _text_input("开始日期", "start_date", values["start_date"], result.field_errors, "date"),
        _text_input("结束日期", "end_date", values["end_date"], result.field_errors, "date"),
        _text_input("持有周期", "holding_period", values["holding_period"], result.field_errors, "number"),
        _text_input("Top N", "selection_top_n", values["selection_top_n"], result.field_errors, "number"),
        _text_input("数据库路径", "db_path", values["db_path"], result.field_errors),
        _text_input("排序字段", "selection_sort_by", values["selection_sort_by"], result.field_errors),
        _select(
            "排序方向",
            "selection_sort_order",
            values["selection_sort_order"],
            [("ASC", "ASC"), ("DESC", "DESC")],
            result.field_errors,
        ),
        _textarea("规则 JSON", "rules_json", values["rules_json"], result.field_errors, rows=10),
    ]
    return (
        '<form class="panel form-panel" method="post" action="/backtests/selection">'
        '<input type="hidden" name="logic" value="AND">'
        '<div class="form-grid">'
        f"{''.join(fields)}"
        "</div>"
        '<div class="form-actions"><button type="submit" data-submit-button>运行普通回测</button>'
        '<div class="run-status" data-run-status hidden>正在执行普通回测，请稍候...</div></div>'
        '<p class="form-hint">排序字段改为手动输入，适合当前字段较少的场景。规则输入沿用现有 JSON 结构，例如 [{"field":"close_price","operator":"gt","value":0}]。</p>'
        "</form>"
    )


def _strategy_form(result: ServiceResult) -> str:
    values = DEFAULT_STRATEGY_FORM | result.form_values
    fields = [
        _text_input("开始日期", "start_date", values["start_date"], result.field_errors, "date"),
        _text_input("结束日期", "end_date", values["end_date"], result.field_errors, "date"),
        _text_input("持有周期", "holding_period", values["holding_period"], result.field_errors, "number"),
        _text_input("最大持仓数", "max_positions", values["max_positions"], result.field_errors, "number"),
        _text_input("单日最大买入数", "max_daily_buys", values["max_daily_buys"], result.field_errors, "number"),
        _text_input("初始资金", "initial_capital", values["initial_capital"], result.field_errors),
        _text_input("单笔仓位比例(%)", "position_size_pct", values["position_size_pct"], result.field_errors),
        _text_input("止盈阈值(%)", "take_profit_pct", values["take_profit_pct"], result.field_errors),
        _text_input("止损阈值(%)", "stop_loss_pct", values["stop_loss_pct"], result.field_errors),
        _text_input("Top N", "selection_top_n", values["selection_top_n"], result.field_errors, "number"),
        _text_input("数据库路径", "db_path", values["db_path"], result.field_errors),
        _text_input("排序字段", "selection_sort_by", values["selection_sort_by"], result.field_errors),
        _select(
            "排序方向",
            "selection_sort_order",
            values["selection_sort_order"],
            [("ASC", "ASC"), ("DESC", "DESC")],
            result.field_errors,
        ),
        _textarea("规则 JSON", "rules_json", values["rules_json"], result.field_errors, rows=10),
    ]
    return (
        '<form class="panel form-panel" method="post" action="/backtests/strategy">'
        '<input type="hidden" name="logic" value="AND">'
        '<div class="form-grid">'
        f"{''.join(fields)}"
        "</div>"
        '<div class="form-actions"><button type="submit" data-submit-button>运行策略回测</button>'
        '<div class="run-status" data-run-status hidden>正在执行策略回测，请稍候...</div></div>'
        '<p class="form-hint">筛选条件放在页面上方，结果集中放在下方。排序字段支持手动输入，便于后续扩展。</p>'
        "</form>"
    )


def _selection_results(result: ServiceResult) -> str:
    if result.payload is None:
        return (
            '<section class="panel result-panel"><h2>结果预览</h2>'
            '<div class="empty-state">提交参数后，这里会显示汇总指标、收益分布和逐笔明细。</div></section>'
        )

    payload = result.payload
    summary = payload["summary"]
    cards = _summary_cards(
        [
            ("命中数", summary["hit_count"]),
            ("已完成交易", summary["completed_count"]),
            ("未完成交易", summary["incomplete_count"]),
            ("胜率(%)", summary["win_rate_pct"]),
            ("平均收益(%)", summary["average_return_pct"]),
            ("交易日数", summary["trade_days"]),
        ]
    )
    empty_html = (
        '<div class="alert alert-info">本次普通回测没有命中任何记录，请调整时间区间、排序或规则后重试。</div>'
        if payload["empty_result"]
        else ""
    )
    details_table = _table(
        [
            ("symbol", "代码"),
            ("name", "名称"),
            ("selection_date", "选股日"),
            ("buy_date", "买入日"),
            ("sell_date", "卖出日"),
            ("buy_price", "买入价"),
            ("sell_price", "卖出价"),
            ("return_pct", "收益率(%)"),
            ("status", "状态"),
            ("incomplete_reason", "未完成原因"),
        ],
        payload["details"],
        "暂无逐笔回测明细。",
    )
    return (
        '<section class="panel result-panel"><h2>普通回测结果</h2>'
        f"{empty_html}{cards}"
        '<div class="result-columns">'
        '<section><h3>收益分布</h3>'
        f"{_distribution_table(payload['return_distribution'])}</section>"
        '<section><h3>逐笔明细</h3>'
        f"{details_table}</section></div></section>"
    )


def _strategy_results(result: ServiceResult) -> str:
    if result.payload is None:
        return (
            '<section class="panel result-panel"><h2>结果预览</h2>'
            '<div class="empty-state">提交参数后，这里会显示账户汇总、交易记录和期末持仓。</div></section>'
        )

    payload = result.payload
    summary = payload["summary"]
    cards = _summary_cards(
        [
            ("当前现金", summary["current_cash_balance"]),
            ("当前市值", summary["current_market_value"]),
            ("总资产", summary["current_total_assets"]),
            ("累计收益(%)", summary["total_return_pct"]),
            ("年化收益(%)", summary["annualized_return_pct"]),
            ("已平仓交易", summary["closed_trade_count"]),
        ]
    )
    empty_html = (
        '<div class="alert alert-info">本次策略回测没有产生任何成交记录，请检查规则、资金参数或区间设置。</div>'
        if payload["empty_result"]
        else ""
    )
    closed_trades = _table(
        [
            ("symbol", "代码"),
            ("name", "名称"),
            ("selection_date", "选股日"),
            ("buy_date", "买入日"),
            ("sell_date", "卖出日"),
            ("buy_amount", "买入金额"),
            ("sell_amount", "卖出金额"),
            ("return_pct", "收益率(%)"),
            ("exit_reason", "退出原因"),
        ],
        payload["closed_trades"],
        "暂无已平仓交易。",
    )
    open_positions = _table(
        [
            ("symbol", "代码"),
            ("name", "名称"),
            ("buy_date", "买入日"),
            ("buy_amount", "买入金额"),
            ("sell_amount", "期末估值"),
            ("return_pct", "浮动收益率(%)"),
            ("exit_reason", "状态"),
        ],
        payload["open_positions"],
        "期末没有未平仓持仓。",
    )
    return (
        '<section class="panel result-panel"><h2>策略回测结果</h2>'
        f"{empty_html}{cards}"
        '<div class="result-columns">'
        f'<section><h3>已平仓交易</h3>{closed_trades}</section>'
        f'<section><h3>期末未平仓</h3>{open_positions}</section>'
        "</div>"
        '<p class="form-hint">当前结果未计入佣金和印花税，和现有 CLI 口径保持一致。</p>'
        "</section>"
    )


def render_selection_page(result: ServiceResult) -> str:
    template = load_template("page.html")
    return template.safe_substitute(
        title="普通回测",
        page_title="普通回测",
        subtitle="顶部导航用于在普通回测和策略回测之间切换，筛选条件集中放在页面上方，结果显示在下方。",
        navigation=_nav("selection"),
        global_errors=_error_list(result.global_errors),
        form_html=_selection_form(result),
        result_html=_selection_results(result),
    )


def render_strategy_page(result: ServiceResult) -> str:
    template = load_template("page.html")
    return template.safe_substitute(
        title="策略回测",
        page_title="策略回测",
        subtitle="策略参数区放在上方，账户汇总、成交记录和期末持仓统一收在下方，便于连续查看。",
        navigation=_nav("strategy"),
        global_errors=_error_list(result.global_errors),
        form_html=_strategy_form(result),
        result_html=_strategy_results(result),
    )
