-- ================================================================
-- 연기금 자동매매 봇 - SQLite 스키마
-- ================================================================

-- 일자별 종목별 연기금 매매 누적치
-- 장 마감 후 확정치로 보정. 연일매수 판정에 사용.
CREATE TABLE IF NOT EXISTS pension_daily (
    trade_date    TEXT NOT NULL,           -- 'YYYY-MM-DD'
    stock_code    TEXT NOT NULL,
    stock_name    TEXT NOT NULL,
    buy_amount    INTEGER NOT NULL DEFAULT 0,    -- 매수금액 (원)
    sell_amount   INTEGER NOT NULL DEFAULT 0,    -- 매도금액 (원)
    net_amount    INTEGER NOT NULL DEFAULT 0,    -- 순매수 (= buy - sell)
    is_confirmed  INTEGER NOT NULL DEFAULT 0,    -- 0=잠정, 1=장마감 확정
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (trade_date, stock_code)
);
CREATE INDEX IF NOT EXISTS idx_pension_daily_code ON pension_daily(stock_code);
CREATE INDEX IF NOT EXISTS idx_pension_daily_date ON pension_daily(trade_date);

-- 트리거 발동 기록 (멱등성 보장 + 감사 로그)
CREATE TABLE IF NOT EXISTS triggers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date    TEXT NOT NULL,
    stock_code    TEXT NOT NULL,
    stock_name    TEXT NOT NULL,
    trigger_type  TEXT NOT NULL,           -- 'NEW_BUY' | 'HOLD_WARN' | 'AUTO_SELL' | 'FAILSAFE_SELL' | 'CONSECUTIVE_BUY'
    pension_amount INTEGER NOT NULL,        -- 연기금 매수/매도 금액
    triggered_at  TEXT NOT NULL,
    payload_json  TEXT                     -- 추가 정보 JSON
);
CREATE INDEX IF NOT EXISTS idx_triggers_dedup ON triggers(trade_date, stock_code, trigger_type);

-- 주문 이력
CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date      TEXT NOT NULL,
    stock_code      TEXT NOT NULL,
    stock_name      TEXT NOT NULL,
    order_no        TEXT,                  -- 키움 반환 주문번호
    side            TEXT NOT NULL,         -- 'SELL'
    order_type      TEXT NOT NULL,         -- 'PRIMARY' | 'FAILSAFE'
    price           INTEGER NOT NULL,
    qty             INTEGER NOT NULL,
    filled_qty      INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL,         -- 'PENDING' | 'PARTIAL' | 'FILLED' | 'CANCELED' | 'FAILED'
    placed_at       TEXT NOT NULL,
    last_updated_at TEXT NOT NULL,
    parent_order_id INTEGER,               -- FAILSAFE의 경우 PRIMARY 참조
    note            TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_orderno ON orders(order_no);

-- 일일 매도 한도 추적 (config의 MAX_SELL_* 와 비교)
CREATE TABLE IF NOT EXISTS daily_sell_totals (
    trade_date    TEXT NOT NULL,
    stock_code    TEXT NOT NULL,           -- '_TOTAL_' = 전체 합계
    total_amount  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (trade_date, stock_code)
);

-- 일일 매수 한도 추적 (config의 MAX_BUY_* 와 비교)
CREATE TABLE IF NOT EXISTS daily_buy_totals (
    trade_date    TEXT NOT NULL,
    stock_code    TEXT NOT NULL,           -- '_TOTAL_' = 전체 합계
    total_amount  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (trade_date, stock_code)
);

-- 시작 시점 보유종목 스냅샷 (보유 여부 판정용)
CREATE TABLE IF NOT EXISTS holdings (
    snapshot_date TEXT NOT NULL,
    stock_code    TEXT NOT NULL,
    stock_name    TEXT NOT NULL,
    qty           INTEGER NOT NULL,
    avg_price     INTEGER NOT NULL,
    PRIMARY KEY (snapshot_date, stock_code)
);
