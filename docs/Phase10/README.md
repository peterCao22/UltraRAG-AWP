# Phase 10 —— 多租户 + KB 共享 + 跨租户检索

> **状态**：方向锚点（2026-05-16），详细计划待 Phase 9 完成后展开
> **前置**：[Phase 8](../Phase8/README.md) + [Phase 9](../Phase9/README.md) 全部完成
> **参考实现**：`D:\Peter2025\myCursor\WeKnora` 全套多租户模型
> - 租户/用户表：`internal/types/tenant.go` + `user.go`
> - 鉴权服务：`internal/application/service/tenant.go` + `user.go`
> - 共享表：`internal/application/repository/kbshare.go`
> - 跨租户检索：`internal/application/repository/knowledge.go:459` `SearchKnowledgeInScopes`
> - chunk 过滤：`internal/application/repository/chunk.go:776` `WHERE tenant_id = ?`

---

## 一、阶段目标

把 custom_app 从「单租户内网工具」升级到「多组织共享平台」：

1. **多租户隔离**：每个用户归属一个组织（tenant），数据按 tenant 强隔离
2. **KB 共享机制**：A 组织的 KB 可授权给 B 组织只读访问（参考 WeKnora 的 `kb_shares` 表）
3. **跨租户检索**：用户一次 query 可在「自有 KB + 共享 KB」范围内联邦检索
4. **鉴权与审计**：登录、权限、操作日志

---

## 二、为什么放在最后

| 维度 | 说明 |
|------|------|
| **横切关注点** | 多租户改造影响所有 Repository、所有 API、所有检索路径——典型「全局打补丁」 |
| **如果先做** | Phase 8/9 所有新代码都要按多租户语义重新写，等于做两遍 |
| **业务紧迫性低** | 当前 AGV/IFS 是内网单租户场景，多租户不是刚需 |
| **改动深度大** | 涉及鉴权层、所有数据访问层、Qdrant payload、Neo4j property、API 中间件、前端登录态 |
| **不需要评测验证** | 这是架构性改造，正确性靠测试覆盖而非评测分数 |

---

## 三、现状基线

### 3.1 已经预留的部分

| 位置 | 内容 | 说明 |
|------|------|------|
| `knowledge_bases.tenant_id` | INTEGER 字段已存在 | 但全填默认值 `1`，无任何过滤逻辑 |
| `chat_models.tenant_id` | 同上（Phase 7 加） | 同上 |
| `kb_documents.tenant_id` | 部分表已有 | 不完整，各表不一致 |

Phase 7 PLAN §三明确写了：「MVP 期 hardcode `g.tenant_id = 1`」——预留字段，等 Phase 10 真正启用。

### 3.2 完全缺失的部分

- **users 表**：没有
- **organizations / tenants 表**：没有
- **鉴权中间件**：没有（仅 admin token 简单校验）
- **kb_shares 表**：没有
- **跨租户检索**：没有
- **登录页**：没有
- **Session/JWT**：没有

### 3.3 Phase 5 + Phase 7 留下的对齐工作

| 数据栈 | 当前 tenant 处理 | Phase 10 需要 |
|--------|----------------|--------------|
| Postgres（awprag） | 字段已存，未用 | 全 Repository 加 `WHERE tenant_id = ?` |
| Qdrant collection | `custom_app__<kb_id>` | payload 加 `tenant_id`；或 collection 命名加前缀 |
| Neo4j | 节点已有 `kb_id` property | 加 `tenant_id` property + 索引 |

---

## 四、子阶段拆分（高层）

### Phase 10.1 — 用户/组织模型 + 鉴权（2 周）

**目标**：能登录、能识别"当前用户属于哪个组织"

**关键工作**：
- 设计 `organizations` / `users` 表（参考 WeKnora `types/tenant.go` + `user.go`）
- 鉴权机制选型：JWT vs Session Cookie vs SSO（接 AD/IFS）—— **待讨论**
- 登录页 + 注册流程（如开放注册）
- Flask 中间件：从请求中解析 user → 注入 `g.tenant_id` / `g.user_id`
- 现有 admin token 机制保留作运维通道

**验收**：未登录访问 `/api/*` 返回 401；登录后 `g.tenant_id` 正确注入

---

### Phase 10.2 — 数据隔离改造（2-3 周）

**目标**：所有数据访问强制按 `tenant_id` 过滤

**关键工作**：
- **Postgres 侧**：每个 Repository 方法加 `tenant_id` 参数，SQL `WHERE tenant_id = ?` 强制
- **Qdrant 侧**：
  - 方案 A：每个 point 的 payload 加 `tenant_id`，检索时 `filter.must=[{key:tenant_id, match:N}]`
  - 方案 B：collection 命名空间化为 `custom_app__<tenant_id>__<kb_id>`
  - **倾向 A**（参考 WeKnora；payload 过滤是 Qdrant 强项；避免 collection 数量爆炸）
- **Neo4j 侧**：所有节点加 `tenant_id` property，索引 `(:Entity {tenant_id, kb_id, name})`
- **历史数据迁移**：所有现有数据回填 `tenant_id=1`
- **Repository 单测全部加 tenant 隔离测试**：A 租户查不到 B 租户数据

**验收**：单测覆盖；E2E 验证两个测试租户互相不可见

---

### Phase 10.3 — KB 共享机制（1-2 周）

**目标**：A 组织的 KB 可授权给 B 组织只读访问（参考 WeKnora `kb_shares` 表）

**关键工作**：
- 新建 `kb_shares` 表：`(kb_id, owner_tenant_id, grantee_tenant_id, permission, created_at)`
  - permission：`read` / `read_chat`（可看 + 可问答）/ 后续可扩 `write`
- API：`POST /api/kb/{kb_id}/share` 邀请；`DELETE /api/kb/{kb_id}/share/{tenant_id}` 撤销
- 检索时计算用户的"可访问 KB scope"：`own_kbs + shared_kbs`
- Admin UI：「分享给」管理面板

**验收**：
- A 创建 KB 共享给 B → B 能在自己列表看到（标"共享"）
- B 可查询、不可修改/删除
- A 撤销共享 → B 立刻看不到

---

### Phase 10.4 — 跨租户联邦检索（1 周）

**目标**：用户一次 query 在「自有 KB + 共享 KB」范围内联邦检索

**关键工作**：
- 检索入口接收 `kb_ids: list[str]` 而不是单个 kb_id
- Qdrant 检索：用 `should` 子句匹配多个 collection / 多个 tenant_id payload
- Neo4j 检索：多 kb_id 联合查询
- 结果归一化：不同 KB 来源的 chunk 在最终结果中标注来源 KB
- 参考 WeKnora `knowledge.go:459` `SearchKnowledgeInScopes((tenant_id, kb_id), ...)`

**验收**：用户 query 同时命中自有 KB chunk + 共享 KB chunk；生成答案标注来源

---

## 五、关键设计议题（详细计划阶段必须先定）

### 5.1 租户单位

| 候选 | 适用场景 |
|------|---------|
| **组织/部门** | 工厂多部门、生产/工艺/IT 各管自己 SOP（推测的主流场景） |
| 个人账号 | 每用户一个 tenant，组织通过 KB share 协作 |
| 混合 | tenant = 部门，user 在 tenant 内，KB owner 在 user 粒度 |

**待你确认**：你想要哪种组织模型？

### 5.2 鉴权方式

| 候选 | 优点 | 缺点 |
|------|------|------|
| 独立账号系统（自建 user 表 + bcrypt + Session） | 简单、可控 | 用户要再记一套密码 |
| 接 AD / LDAP | 内网企业标配，单点登录 | 需 IT 部协调 |
| 接 IFS SSO（如有） | 和现有系统统一 | 取决于 IFS 是否提供标准协议（OIDC/SAML） |
| 双轨：内部 AD + 外部独立 | 灵活 | 工作量翻倍 |

**待你确认**：公司有 AD 域吗？IFS 是否提供 SSO？

### 5.3 KB 共享粒度

| 颗粒度 | 复杂度 | 用例 |
|--------|--------|------|
| **KB 级**（推荐） | 低 | A 部门把"AGV 培训库"完整分享给 B 部门 |
| Document 级 | 中 | 只分享 KB 里某几篇文档 |
| Chunk 级 | 高 | 按段落控制（基本不用） |

参考 WeKnora 也只做到 **KB 级**（`kbshare.go`），建议跟随。

### 5.4 权限模型

| 模型 | 说明 |
|------|------|
| **RBAC（角色）** | 用户有角色，角色有权限。Phase 3 已经有 `roles + role_kb_permissions` 表雏形 |
| ABAC（属性） | 按属性动态计算，过于复杂 |
| 直接 ACL | 用户→KB 直接授权 |

Phase 3 已经做了 RBAC 基础，**复用并扩展**——本期不重新设计，加多租户维度即可。

---

## 六、关键风险

| 等级 | 风险 | 缓解 |
|------|------|------|
| 🔴 HIGH | tenant 过滤遗漏 = 数据泄露 | 强制 Repository 层 `tenant_id` 必传参数；CI 加 lint 规则禁止 raw SQL |
| 🔴 HIGH | 历史数据回填出错（全部塞 tenant_id=1） | 迁移脚本写好 + 逐表 verify 行数 |
| 🟡 MED | Qdrant payload 过滤性能 | 给 `tenant_id` 建 payload 索引（Qdrant 支持） |
| 🟡 MED | Neo4j 多 tenant 节点共存数据膨胀 | 索引设计 `(tenant_id, kb_id, name)` 联合，查询走索引 |
| 🟡 MED | 跨租户 KB 共享后，被分享方的检索变慢（要扫多个 collection） | 限制每用户最多共享 N 个 KB；Phase 10.4 评估 |
| 🟡 MED | 现有 Phase 8/9 改动需要兼容多租户 | Phase 10 启动时把 Phase 8/9 新增的所有 Repository 方法补 tenant 过滤 |
| 🟢 LOW | 登录/Session/CSRF 实现细节 | 用成熟库（Flask-Login / itsdangerous），不自己造 |

---

## 七、退出条件（每个子阶段独立）

| 子阶段 | 退出条件 | 不达标处理 |
|--------|---------|-----------|
| 10.1 | 登录可用，`g.tenant_id` 正确注入；admin token 通道仍可走 | 必须达标，否则后续无法做 |
| 10.2 | 两个测试租户 E2E 互不可见 | 必须达标 |
| 10.3 | KB 共享授予/撤销/可见性正确 | 可推迟，Phase 10.2 做完已具备基础多租户 |
| 10.4 | 跨租户联邦检索可用 | 可推迟，可只支持"切租户后查" |

**最坏情况**：Phase 10 至 10.2 收尾——已具备**最基础的多租户隔离**，足以应付"多部门独立使用"的场景；共享和联邦后续按需补。

---

## 八、与既有 Phase 的关系

| Phase | 关系 |
|-------|------|
| Phase 3 | RBAC `roles + role_kb_permissions` 是 Phase 10.2 权限模型的基础 |
| Phase 5.1 | Qdrant + Postgres 是 Phase 10.2 的数据隔离改造对象 |
| Phase 5.2 | Neo4j KG 节点全部加 tenant_id property |
| Phase 7 | `chat_models.tenant_id` 字段已预留；Phase 10 启用 |
| Phase 8.x | 评测、BM25、IRCoT 全部改造为按 tenant 范围跑 |
| Phase 9.x | 图片节点、KG 实体都要加 tenant_id |

---

## 九、文档清单（待写）

- [ ] PHASE_10_1_PLAN.md —— 用户/组织模型 + 鉴权（Phase 9 完成后展开）
- [ ] PHASE_10_2_PLAN.md —— 数据隔离改造
- [ ] PHASE_10_3_PLAN.md —— KB 共享机制
- [ ] PHASE_10_4_PLAN.md —— 跨租户联邦检索

**当前文档仅为方向锚点**。Phase 9 完成后再展开子计划。

---

## 十、给未来自己的备忘

写详细计划时**必须先确认**：

1. **租户单位**：组织 vs 部门 vs 个人？（§5.1）
2. **鉴权方式**：自建账号 / AD / SSO？（§5.2）
3. **KB 共享粒度**：KB 级 / Document 级？（§5.3）—— 建议跟 WeKnora 走 KB 级
4. **权限模型**：复用 Phase 3 RBAC 还是重新设计？—— 建议复用
5. **现有 admin token 是否保留**：作为"超级管理员"运维通道
6. **数据迁移策略**：全量回填 tenant_id=1 还是按规则分配到多个租户？
7. **审计日志**：是否需要操作日志（who did what when）？

---

## 十一、估算

| 子阶段 | 工时 |
|--------|------|
| 10.1 用户/组织 + 鉴权 | 2 周 |
| 10.2 数据隔离改造 | 2-3 周 |
| 10.3 KB 共享 | 1-2 周 |
| 10.4 跨租户检索 | 1 周 |
| **合计** | **6-8 周（约 1.5-2 月）** |

**这是个真正的大型 Phase**，比 Phase 8 + Phase 9 加起来都大。如果团队规模有限，可以只做 10.1 + 10.2 收尾，10.3/10.4 按业务需求再说。

---

> **下一步**：等 Phase 9 完成，回头展开 Phase 10.1 详细计划。
