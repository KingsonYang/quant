-- quant schema v1
-- DuckDB-oriented logical DDL for the first version of the A-share daily backtesting system.

CREATE TABLE IF NOT EXISTS security_master (
    security_id BIGINT PRIMARY KEY,
    symbol VARCHAR NOT NULL,
    exchange VARCHAR NOT NULL,
    code VARCHAR NOT NULL,
    name VARCHAR NOT NULL,
    market VARCHAR NOT NULL DEFAULT 'ab',
    board VARCHAR,
    list_status VARCHAR NOT NULL DEFAULT 'listed',
    list_date DATE,
    delist_date DATE,
    currency VARCHAR NOT NULL DEFAULT 'CNY',
    data_source VARCHAR,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(symbol),
    UNIQUE(exchange, code)
);

CREATE TABLE IF NOT EXISTS trading_calendar (
    calendar_id BIGINT PRIMARY KEY,
    market VARCHAR NOT NULL DEFAULT 'ab',
    trade_date DATE NOT NULL,
    is_trading_day BOOLEAN NOT NULL,
    prev_trade_date DATE,
    next_trade_date DATE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(market, trade_date)
);

CREATE TABLE IF NOT EXISTS raw_market_request (
    raw_request_id BIGINT PRIMARY KEY,
    security_id BIGINT,
    source_name VARCHAR NOT NULL,
    endpoint VARCHAR NOT NULL,
    http_method VARCHAR NOT NULL DEFAULT 'GET',
    query_params TEXT,
    requested_at TIMESTAMP NOT NULL,
    request_status VARCHAR NOT NULL,
    error_message TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (security_id) REFERENCES security_master(security_id)
);

CREATE TABLE IF NOT EXISTS raw_market_response (
    raw_response_id BIGINT PRIMARY KEY,
    raw_request_id BIGINT NOT NULL,
    security_id BIGINT,
    fetched_at TIMESTAMP NOT NULL,
    response_format VARCHAR NOT NULL,
    response_body TEXT NOT NULL,
    parse_status VARCHAR NOT NULL,
    parser_version VARCHAR,
    checksum VARCHAR,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (raw_request_id) REFERENCES raw_market_request(raw_request_id),
    FOREIGN KEY (security_id) REFERENCES security_master(security_id)
);

CREATE TABLE IF NOT EXISTS daily_bar (
    security_id BIGINT NOT NULL,
    trade_date DATE NOT NULL,
    ts_epoch BIGINT,
    open_price DECIMAL(18,4) NOT NULL,
    high_price DECIMAL(18,4) NOT NULL,
    low_price DECIMAL(18,4) NOT NULL,
    close_price DECIMAL(18,4) NOT NULL,
    pre_close_price DECIMAL(18,4),
    volume BIGINT,
    amount DECIMAL(20,2),
    change_amount DECIMAL(18,4),
    change_pct DECIMAL(10,4),
    turnover_ratio DECIMAL(10,4),
    ma5_price DECIMAL(18,4),
    ma10_price DECIMAL(18,4),
    ma20_price DECIMAL(18,4),
    ma5_volume BIGINT,
    ma10_volume BIGINT,
    ma20_volume BIGINT,
    open_qfq DECIMAL(18,4),
    high_qfq DECIMAL(18,4),
    low_qfq DECIMAL(18,4),
    close_qfq DECIMAL(18,4),
    adjust_type VARCHAR DEFAULT 'none',
    source_name VARCHAR NOT NULL,
    parser_version VARCHAR,
    ingested_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (security_id, trade_date),
    FOREIGN KEY (security_id) REFERENCES security_master(security_id)
);

CREATE TABLE IF NOT EXISTS ths_daily_snapshot (
    snapshot_id BIGINT PRIMARY KEY,
    security_id BIGINT NOT NULL,
    symbol VARCHAR NOT NULL,
    name VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    source_file TEXT NOT NULL,
    row_number INTEGER NOT NULL,
    raw_payload_json TEXT NOT NULL,
    current_price DECIMAL(18,4),
    open_price DECIMAL(18,4),
    high_price DECIMAL(18,4),
    low_price DECIMAL(18,4),
    pre_close_price DECIMAL(18,4),
    change_amount DECIMAL(18,4),
    change_pct DECIMAL(10,4),
    volume BIGINT,
    amount DECIMAL(20,2),
    turnover_ratio DECIMAL(10,4),
    volume_ratio DECIMAL(18,4),
    total_market_cap DECIMAL(24,2),
    float_market_cap DECIMAL(24,2),
    float_market_cap_tagged DECIMAL(24,2),
    float_ratio DECIMAL(10,4),
    industry VARCHAR,
    sub_industry VARCHAR,
    concepts TEXT,
    limit_type VARCHAR,
    limit_reason TEXT,
    first_limit_time VARCHAR,
    last_limit_time VARCHAR,
    limit_open_count INTEGER,
    limit_seal_ratio DECIMAL(18,4),
    limit_seal_amount DECIMAL(24,2),
    limit_seal_amount_new DECIMAL(24,2),
    limit_seal_volume_new BIGINT,
    consecutive_limit_days INTEGER,
    open_change_pct DECIMAL(10,4),
    auction_turnover_ratio DECIMAL(10,4),
    five_day_change_pct DECIMAL(10,4),
    ten_day_change_pct DECIMAL(10,4),
    twenty_day_change_pct DECIMAL(10,4),
    amplitude_pct DECIMAL(10,4),
    inner_volume BIGINT,
    outer_volume BIGINT,
    inner_outer_ratio DECIMAL(18,4),
    main_net_volume DECIMAL(18,4),
    buy_volume BIGINT,
    sell_volume BIGINT,
    transaction_count BIGINT,
    institutional_trend DECIMAL(18,4),
    total_shares BIGINT,
    float_shares BIGINT,
    net_profit_growth_pct DECIMAL(18,4),
    net_asset_per_share DECIMAL(18,4),
    golden_cross_count INTEGER,
    retail_holder_count DECIMAL(18,4),
    list_date DATE,
    ingested_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(symbol, trade_date),
    FOREIGN KEY (security_id) REFERENCES security_master(security_id)
);

CREATE TABLE IF NOT EXISTS event_record (
    event_id BIGINT PRIMARY KEY,
    security_id BIGINT NOT NULL,
    event_date DATE NOT NULL,
    event_type VARCHAR NOT NULL,
    event_title VARCHAR,
    event_content TEXT,
    event_payload TEXT,
    source_name VARCHAR NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (security_id) REFERENCES security_master(security_id)
);

CREATE TABLE IF NOT EXISTS strategy_definition (
    strategy_id BIGINT PRIMARY KEY,
    strategy_name VARCHAR NOT NULL,
    strategy_key VARCHAR NOT NULL,
    strategy_type VARCHAR NOT NULL,
    description TEXT,
    parameter_schema TEXT,
    version VARCHAR NOT NULL DEFAULT 'v1',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(strategy_key, version)
);

CREATE TABLE IF NOT EXISTS backtest_run (
    run_id BIGINT PRIMARY KEY,
    strategy_id BIGINT NOT NULL,
    run_kind VARCHAR NOT NULL,
    run_name VARCHAR NOT NULL,
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    benchmark_symbol VARCHAR,
    universe_definition TEXT,
    parameter_snapshot TEXT NOT NULL,
    initial_cash DECIMAL(20,2) NOT NULL,
    commission_rate DECIMAL(10,6) NOT NULL DEFAULT 0,
    tax_rate DECIMAL(10,6) NOT NULL DEFAULT 0,
    slippage_rate DECIMAL(10,6) NOT NULL DEFAULT 0,
    run_status VARCHAR NOT NULL,
    started_at TIMESTAMP,
    finished_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (strategy_id) REFERENCES strategy_definition(strategy_id)
);

CREATE TABLE IF NOT EXISTS run_metric (
    run_metric_id BIGINT PRIMARY KEY,
    run_id BIGINT NOT NULL,
    metric_name VARCHAR NOT NULL,
    metric_value DOUBLE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(run_id, metric_name),
    FOREIGN KEY (run_id) REFERENCES backtest_run(run_id)
);

CREATE TABLE IF NOT EXISTS run_artifact (
    artifact_id BIGINT PRIMARY KEY,
    run_id BIGINT NOT NULL,
    artifact_name VARCHAR NOT NULL,
    artifact_type VARCHAR NOT NULL,
    relative_path TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(run_id, artifact_name),
    FOREIGN KEY (run_id) REFERENCES backtest_run(run_id)
);

CREATE TABLE IF NOT EXISTS backtest_order (
    order_id BIGINT PRIMARY KEY,
    run_id BIGINT NOT NULL,
    security_id BIGINT NOT NULL,
    submitted_date DATE NOT NULL,
    intended_exec_date DATE,
    side VARCHAR NOT NULL,
    order_type VARCHAR NOT NULL,
    quantity BIGINT NOT NULL,
    target_weight DECIMAL(10,6),
    status VARCHAR NOT NULL,
    reject_reason VARCHAR,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (run_id) REFERENCES backtest_run(run_id),
    FOREIGN KEY (security_id) REFERENCES security_master(security_id)
);

CREATE TABLE IF NOT EXISTS backtest_fill (
    fill_id BIGINT PRIMARY KEY,
    order_id BIGINT NOT NULL,
    run_id BIGINT NOT NULL,
    security_id BIGINT NOT NULL,
    fill_date DATE NOT NULL,
    fill_price DECIMAL(18,4) NOT NULL,
    fill_quantity BIGINT NOT NULL,
    gross_amount DECIMAL(20,2) NOT NULL,
    commission DECIMAL(20,2) NOT NULL DEFAULT 0,
    tax DECIMAL(20,2) NOT NULL DEFAULT 0,
    slippage_cost DECIMAL(20,2) NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (order_id) REFERENCES backtest_order(order_id),
    FOREIGN KEY (run_id) REFERENCES backtest_run(run_id),
    FOREIGN KEY (security_id) REFERENCES security_master(security_id)
);

CREATE TABLE IF NOT EXISTS position_snapshot (
    position_snapshot_id BIGINT PRIMARY KEY,
    run_id BIGINT NOT NULL,
    trade_date DATE NOT NULL,
    security_id BIGINT NOT NULL,
    quantity BIGINT NOT NULL,
    sellable_quantity BIGINT NOT NULL,
    avg_cost DECIMAL(18,4) NOT NULL,
    close_price DECIMAL(18,4) NOT NULL,
    market_value DECIMAL(20,2) NOT NULL,
    unrealized_pnl DECIMAL(20,2) NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(run_id, trade_date, security_id),
    FOREIGN KEY (run_id) REFERENCES backtest_run(run_id),
    FOREIGN KEY (security_id) REFERENCES security_master(security_id)
);

CREATE TABLE IF NOT EXISTS equity_snapshot (
    equity_snapshot_id BIGINT PRIMARY KEY,
    run_id BIGINT NOT NULL,
    trade_date DATE NOT NULL,
    cash DECIMAL(20,2) NOT NULL,
    market_value DECIMAL(20,2) NOT NULL,
    total_equity DECIMAL(20,2) NOT NULL,
    daily_pnl DECIMAL(20,2) NOT NULL DEFAULT 0,
    cumulative_return DECIMAL(12,6) NOT NULL DEFAULT 0,
    turnover DECIMAL(12,6),
    max_drawdown DECIMAL(12,6),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(run_id, trade_date),
    FOREIGN KEY (run_id) REFERENCES backtest_run(run_id)
);

