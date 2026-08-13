# 猫超 RPA 后端接口说明

更新时间：2026-08-12

## 部署角色

- `api_server.py`：HTTP API 服务，适合用 NSSM 安装为 Windows 服务。
- `rpa_worker.py`：RPA 执行 worker，负责启动/接管 Chrome，建议用 Windows 任务计划程序在固定用户登录会话中启动。
- `maochao_rpa.py`：现有 RPA CLI，不直接暴露给前端。
- `account_store.py`：账号加密 SQLite 库。
- `web/`：浏览器管理台静态文件，由 `api_server.py` 同源托管。

## 端口与登录态规则

已实现硬约束：

- 一个账号配置一个固定 `port`。
- 一个账号配置一个固定 `profile_dir`。
- 账号库禁止重复 `port`，也禁止重复 `profile_dir`。
- worker 执行前会写入 `account_locks`。
- 同一个 `account_key`、`port`、`profile_dir` 在锁释放前不能被第二个 run 占用。
- RPA 层使用 `--remote-debugging-port=<account.port>` 和 `--user-data-dir=<account.profile_dir>` 接管 Chrome。

当前账号示例：

| account_key | port | profile_dir |
|---|---:|---|
| `tmall_inventory_01` | `9221` | `browser_profiles/tmall_inventory_01` |
| `tmall_common_01` | `9230` | `browser_profiles/tmall_common_01` |

不要让多个账号共用同一个 `profile_dir`，也不要让同一账号配置多个端口，否则登录态和验证码会变得不可控。

## 启动

API 服务：

```bash
python -u api_server.py
```

默认监听：

```text
http://0.0.0.0:8000
```

worker：

```bash
python -u rpa_worker.py
```

Windows 建议：

- `api_server.py` 打包成 `backend.exe`，由 NSSM 自启。
- `rpa_worker.py` 打包成 `worker.exe`，由任务计划程序在固定用户登录会话中自启。
- Chrome profile 放在 `D:\rpa_chrome_profiles\<account_key>`。
- 输出文件放在 `D:\outputs` 或 API 配置的 `data_root`。

## 接口

### GET `/health`

兼容部署探活。

### GET `/api/health`

返回 API 基础状态。

### GET `/api/ready`

检查配置、账号库、数据目录、日志目录、截图目录。

### GET `/api/worker`

返回 worker 心跳和队列概览：

- `worker_online`
- `running_run_id`
- `pending_count`
- `running_count`
- `heartbeat_at`
- `heartbeat_age_seconds`
- `worker_pid`

worker 每 2 秒写入 `backend/worker_heartbeat.json`。超过 8 秒没有更新时，接口返回 `worker_online=false`。API 服务在线不代表 worker 在线。

### GET `/api/tasks`

返回任务列表。

字段：

- `task_key`
- `title`
- `file_task_text`

### GET `/api/accounts`

返回账号列表。密码不会返回明文。

字段：

- `key`
- `name`
- `username`
- `username_set`
- `password_set`
- `port`
- `profile_dir`
- `download_dir`
- `supplier_names`
- `tasks`
- `note`
- `xpath_vars`
- `selector_overrides`
- `enabled`
- `browser_status`
- `locked_by_run_id`

### POST `/api/accounts`

新增或覆盖账号。

### PATCH `/api/accounts/{account_key}`

更新账号字段。

### POST `/api/runs`

创建运行任务。

请求：

```json
{
  "task_keys": ["realtime-inventory", "pincang-detail"],
  "account_keys": ["tmall_inventory_01"],
  "force_account_tasks": true,
  "headed": true
}
```

返回 run 详情，初始状态为 `pending`。

状态：

- `pending`
- `running`
- `succeeded`
- `failed`

### GET `/api/runs`

返回所有 run，按创建时间倒序。

### GET `/api/runs/{run_id}`

返回单个 run。

### POST `/api/runs/{run_id}/cancel`

仅允许取消 `pending` 状态的排队任务。`running`、`succeeded`、`failed`、`cancelled` 不允许取消。

### POST `/api/runs/{run_id}/pause`

暂停任务：

- `pending`：立即从队列移出并变为 `paused`。
- `running`：记录暂停请求，当前账号/当前子任务完成后变为 `paused`。
- 不强制终止 Chrome 或正在进行的文件下载。

### POST `/api/runs/{run_id}/resume`

恢复任务：

- `paused`：重新进入队列，排在当前队列末尾。
- `running` 且存在暂停请求：取消暂停请求，继续执行。

### POST `/api/runs/{run_id}/move-up`

将 `pending` 任务在队列中上移一位。

### POST `/api/runs/{run_id}/move-down`

将 `pending` 任务在队列中下移一位。

### GET `/api/runs/{run_id}/logs`

返回 worker 日志文本。

### GET `/api/runs/{run_id}/errors`

返回该 run 的错误。

### GET `/api/runs/{run_id}/files/download`

打包下载该 run 产生的 raw / cleaned 文件，返回 zip。

### GET `/api/errors`

返回所有 run 中的错误。

### GET `/api/files`

返回 `data_root` 下的文件索引。

字段：

- `file_id`
- `name`
- `path`
- `size`
- `updated_at`
- `run_id`：能匹配到运行结果时返回对应 run，前端用于显示“大任务”编号

### GET `/api/files/{file_id}/download`

下载文件。

### GET `/api/screenshots/{screenshot_id}`

下载或查看截图。

## 前端轮询建议

1. 调 `POST /api/runs` 创建 run。
2. 每 2 秒调 `GET /api/runs/{run_id}`。
3. 状态为 `succeeded` 时展示成功和文件。
4. 状态为 `failed` 时展示 `GET /api/runs/{run_id}/errors` 和日志。

## 当前限制

- 第一版 worker 是单进程轮询队列。
- 暂停是协作式暂停，只在账号/任务边界生效。
- 同账号强制串行。
- 不建议同一台主机并发启动多个 worker。
- 账号密码更新后 API 立即写入账号库，已启动中的 run 不会中途切换配置。
- 打开 `http://后端电脑IP:8000` 进入 Web 管理台，`/docs` 仍保留给维护人员。
