# Phase 2 — RBAC 权限设计

> 目标：以**角色（Role）**为核心，管理企业内部用户对知识库的访问权限，替代多租户隔离方案。  
> 设计原则：轻量、内部使用友好，支持"一个用户属于一个角色、一个角色可访问多个知识库"。

---

## 1. 模型概念

```
用户（User）→ 角色（Role）→ 知识库权限（role_kb_permissions）→ 知识库（KB）
```

- 每个用户绑定一个角色（Phase 2 简化：不强制约束，可无 user 表直接用 role_id 过滤）
- 每个角色可绑定多个知识库，每条绑定带 `access_level`（read/write/admin）
- 同一知识库可授权给多个角色

---

## 2. 数据表结构

### 2.1 `roles`

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增主键 |
| role_id | TEXT UNIQUE | 角色唯一 ID（`role_xxx`） |
| name | TEXT UNIQUE | 角色名称（如 `editor`, `viewer`） |
| description | TEXT | 角色描述 |
| created_at | TEXT | 创建时间 |
| updated_at | TEXT | 更新时间 |

### 2.2 `role_kb_permissions`

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增主键 |
| role_id | TEXT | 角色 ID（外键 → roles.role_id） |
| kb_id | TEXT | 知识库 ID（外键 → knowledge_bases.kb_id） |
| access_level | TEXT | 权限级别：`read` / `write` / `admin` |
| created_at | TEXT | 创建时间 |
| updated_at | TEXT | 更新时间 |

约束：`UNIQUE(role_id, kb_id)`（同角色同知识库只有一条权限记录，更新时幂等）

---

## 3. 权限级别说明

| access_level | 含义 |
|--------------|------|
| `read` | 只可查询该知识库（`/api/chat`） |
| `write` | 可上传文档、触发 ingest |
| `admin` | 可管理知识库元数据（创建/删除/更新） |

> Phase 2 仅建模，不强制在 API 层做鉴权拦截。Phase 2.5 引入 API Key 后再联动。

---

## 4. API 接口

### 4.1 角色管理

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/roles` | 创建角色 |
| GET | `/api/roles` | 列出所有角色 |
| GET | `/api/roles/{role_id}` | 角色详情 |
| DELETE | `/api/roles/{role_id}` | 删除角色（同时级联删除权限） |

### 4.2 权限绑定

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/roles/{role_id}/permissions` | 授予角色对某知识库的权限（幂等） |
| GET | `/api/roles/{role_id}/permissions` | 列出角色的所有知识库权限 |
| DELETE | `/api/roles/{role_id}/permissions/{kb_id}` | 撤销权限 |

### 4.3 知识库列表按角色过滤

```
GET /api/kb?role_id=role_xxx
```

只返回该角色有权限的知识库，适用于前端根据当前用户角色展示可用知识库。

---

## 5. 示例流程

```bash
# 1. 创建角色
POST /api/roles  {"name": "agv_operator", "description": "AGV 运维人员"}
→ {"role_id": "role_abc123", ...}

# 2. 创建知识库
POST /api/kb  {"kb_id": "agv_manual", "name": "AGV 操作手册"}

# 3. 将知识库授权给角色
POST /api/roles/role_abc123/permissions  {"kb_id": "agv_manual", "access_level": "read"}

# 4. 前端按角色获取可用知识库
GET /api/kb?role_id=role_abc123
→ [{"kb_id": "agv_manual", ...}]
```

---

## 6. 与 WeKnora 多租户方案的对比

| 维度 | WeKnora 多租户 | UltraRAG RBAC |
|------|----------------|---------------|
| 隔离粒度 | tenant_id 硬隔离 | 角色-知识库软绑定 |
| 适用场景 | SaaS 多客户 | 企业内部多部门/角色 |
| 复杂度 | 高（所有查询带 tenant 约束） | 低（角色过滤为可选参数） |
| 扩展方向 | 用户+权限系统 | API Key + role_id 鉴权 |

---

## 7. Phase 2.5 扩展方向

- 引入 `users` 表（user_id, username, role_id, api_key_hash）
- 每个请求携带 API Key，服务端解析 role_id
- 对 write/admin 操作在 API 层做权限拦截
- 支持一用户多角色（`user_roles` 中间表）
