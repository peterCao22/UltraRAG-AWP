# Phase 2 — 知识库 API 设计（基础版）

> 目标：提供最小可用的知识库管理 API，支撑多库元数据维护与入库任务触发。

---

## 1. 通用约定

- Base URL：`/api`
- 响应统一包含：
  - `request_id`：请求追踪 ID
- 错误结构：
  - `{ "error": "...", "code": "...", "request_id": "..." }`

---

## 2. API 列表（Phase 2 基础）

### 2.1 创建知识库

- `POST /api/kb`

请求体：

```json
{
  "kb_id": "agv_demo_2",
  "name": "AGV Demo 2",
  "description": "for testing"
}
```

成功响应：

```json
{
  "request_id": "req_xxx",
  "data": {
    "kb_id": "agv_demo_2",
    "status": "active"
  }
}
```

---

### 2.2 知识库列表

- `GET /api/kb`
- 默认不返回 `archived`，可通过 `include_archived=true` 查看

成功响应：

```json
{
  "request_id": "req_xxx",
  "data": [
    {
      "kb_id": "agv_demo",
      "name": "AGV Demo",
      "status": "active",
      "last_indexed_at": "2026-03-31T10:00:00Z"
    }
  ]
}
```

---

### 2.3 知识库详情

- `GET /api/kb/{kb_id}`
- 默认不返回 `archived`，可通过 `include_archived=true` 查看

---

### 2.4 删除知识库

- `DELETE /api/kb/{kb_id}`
- 默认策略：软删除（`status=archived`），避免误删数据
- 软删除后，默认列表/详情不可见（与删除语义一致）

---

### 2.5 触发入库任务

- `POST /api/kb/{kb_id}/ingest`

请求体（基础版可为空）：

```json
{
  "force_reindex": false,
  "async": false
}
```

成功响应：

- 同步（`async=false`）：直接返回 `success`/`failed`
- 异步（`async=true`）：返回 `pending`，由后台线程执行

```json
{
  "request_id": "req_xxx",
  "data": {
    "job_id": "job_xxx",
    "status": "success"
  }
}
```

---

### 2.6 查询任务列表

- `GET /api/kb/{kb_id}/jobs`

---

### 2.6.1 查询任务详情

- `GET /api/kb/{kb_id}/jobs/{job_id}`
- 当前返回中包含：
  - 原始字段：`status`、`retry_count`、`last_error`、`payload_json`、`result_json`
  - 便捷字段：`payload`、`result`、`summary`

---

### 2.7 查询文档记录

- `GET /api/kb/{kb_id}/documents`

---

### 2.8 取消任务（骨架）

- `POST /api/kb/{kb_id}/jobs/{job_id}/cancel`
- 说明：当前为状态级取消，已完成任务不会被回滚

---

### 2.9 重试任务（骨架）

- `POST /api/kb/{kb_id}/jobs/{job_id}/retry`
- 说明：复用原任务参数，`retry_count + 1` 后重新执行

---

### 2.10 手动运行任务

- `POST /api/kb/{kb_id}/jobs/{job_id}/run`
- 说明：对 `pending/cancelled/failed` 任务进行手动重新执行，支持 `async` 参数

---

## 3. 与现有接口协同

- `/api/chat`、`/api/chat/markdown` 继续保留
- `kb_id` 参数继续用于多库切换
- 若 `kb_id` 不存在，返回 404（统一错误结构）

---

## 4. 后续扩展（Phase 2.5+）

- 增加租户头/上下文（`tenant_id`）
- 增加批量文档上传与进度流
- 将当前线程池异步升级为队列执行（限流、重试、取消）
