-- GeminiGen 对外平台数据库表
-- 在现有 quote_iw 库中新增以下表

USE quote_iw;

-- 用户表
CREATE TABLE IF NOT EXISTS platform_users (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    email         VARCHAR(255) NOT NULL UNIQUE,
    username      VARCHAR(100),
    password_hash VARCHAR(255) NOT NULL,
    balance       DECIMAL(12, 4) NOT NULL DEFAULT 0.0000,
    total_tasks   INT NOT NULL DEFAULT 0,
    is_active     TINYINT NOT NULL DEFAULT 1,
    is_admin      TINYINT NOT NULL DEFAULT 0,
    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- API Key 表
CREATE TABLE IF NOT EXISTS api_keys (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    key_name    VARCHAR(100) NOT NULL DEFAULT 'default',
    key_value   VARCHAR(80) NOT NULL UNIQUE,
    is_active   TINYINT NOT NULL DEFAULT 1,
    total_calls INT NOT NULL DEFAULT 0,
    last_used_at DATETIME,
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES platform_users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 生成任务表
CREATE TABLE IF NOT EXISTS gen_tasks (
    id                BIGINT AUTO_INCREMENT PRIMARY KEY,
    task_id           VARCHAR(64) NOT NULL UNIQUE,
    user_id           BIGINT NOT NULL,
    api_key_id        BIGINT,
    model             VARCHAR(50) NOT NULL DEFAULT 'nano-banana-2',
    reference_image_url VARCHAR(1000),
    prompt_text       TEXT,
    aspect_ratio      VARCHAR(10) NOT NULL DEFAULT '1:1',
    output_format     VARCHAR(10) NOT NULL DEFAULT 'png',
    resolution        VARCHAR(10) NOT NULL DEFAULT '1K',
    result_image_url  VARCHAR(1000),
    status            VARCHAR(20) NOT NULL DEFAULT 'pending',
    -- pending | processing | success | failed
    cost              DECIMAL(10, 4),
    error_msg         VARCHAR(500),
    created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES platform_users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Schema migration: add new columns to existing gen_tasks if needed
DROP PROCEDURE IF EXISTS _migrate_gen_tasks;
CREATE PROCEDURE _migrate_gen_tasks()
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='gen_tasks' AND COLUMN_NAME='aspect_ratio'
    ) THEN
        ALTER TABLE gen_tasks
            ADD COLUMN aspect_ratio VARCHAR(10) NOT NULL DEFAULT '1:1',
            ADD COLUMN output_format VARCHAR(10) NOT NULL DEFAULT 'png',
            ADD COLUMN resolution VARCHAR(10) NOT NULL DEFAULT '1K',
            ADD COLUMN reference_image_url VARCHAR(1000);
    END IF;
END;
CALL _migrate_gen_tasks();
DROP PROCEDURE IF EXISTS _migrate_gen_tasks;

-- 余额流水表
CREATE TABLE IF NOT EXISTS balance_transactions (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id       BIGINT NOT NULL,
    amount        DECIMAL(10, 4) NOT NULL,   -- 正=充值, 负=扣费
    type          VARCHAR(20) NOT NULL,       -- recharge | deduct | refund
    task_id       VARCHAR(64),
    note          VARCHAR(300),
    balance_after DECIMAL(12, 4),
    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES platform_users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 充值订单表
CREATE TABLE IF NOT EXISTS recharge_orders (
    id             BIGINT AUTO_INCREMENT PRIMARY KEY,
    order_id       VARCHAR(64) NOT NULL UNIQUE,
    user_id        BIGINT NOT NULL,
    amount         DECIMAL(10, 2) NOT NULL,
    status         VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending | paid
    note           VARCHAR(200),
    created_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    paid_at        DATETIME,
    FOREIGN KEY (user_id) REFERENCES platform_users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
