# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

ChatGPT 批量自动注册工具，支持 DuckMail 和 Cloudflare Email Worker 两种邮箱后端。

## 运行命令

```bash
python chatgpt_register.py
```

## 依赖安装

```bash
pip install curl_cffi
```

## 配置

所有配置通过 `config.json` 或环境变量设置：

| 配置项 | 环境变量 | 说明 |
|--------|----------|------|
| `email_backend` | `EMAIL_BACKEND` | 邮箱后端：`duckmail` 或 `cfemail` |
| `duckmail_bearer` | `DUCKMAIL_BEARER` | DuckMail API 密钥 |
| `cfemail_url` | `CFEMAIL_URL` | CF Email Worker URL |
| `cfemail_password` | `CFEMAIL_PASSWORD` | CF Email Worker 访问密码 |
| `total_accounts` | `TOTAL_ACCOUNTS` | 注册账号数量 |
| `proxy` | `PROXY` | HTTP 代理地址 |
| `enable_oauth` | `ENABLE_OAUTH` | 启用 OAuth 流程 |
| `oauth_required` | `OAUTH_REQUIRED` | 是否必须完成 OAuth |
| `oauth_client_id` | `OAUTH_CLIENT_ID` | OAuth 客户端 ID |
| `oauth_redirect_uri` | `OAUTH_REDIRECT_URI` | OAuth 回调地址 |
| `ak_file` | `AK_FILE` | Access Key 文件 |
| `rk_file` | `RK_FILE` | Refresh Key 文件 |
| `token_json_dir` | `TOKEN_JSON_DIR` | Token JSON 输出目录 |
| `output_file` | - | 注册成功账号输出文件 |

## 代码架构

- **`ChatGPTRegister`**: 核心注册类，包含完整注册流程
- **`SentinelTokenGenerator`**: PoW 验证码生成器
- **`create_temp_email`**: 创建临时邮箱（根据配置选择后端）
- **`wait_for_verification_email`**: 等待并获取邮箱验证码
- **`run_batch`**: 批量并发注册入口函数
- **`_register_one`**: 单账号注册逻辑

### 邮箱后端

- `duckmail`: 使用 DuckMail 临时邮箱服务
- `cfemail`: 使用 Cloudflare Email Worker 自建邮箱
