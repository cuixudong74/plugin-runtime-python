# 插件运行时

本仓库在现有 FastAPI 服务之上提供一套插件运行时（`plugin_runtime` 包），让每项
能力都能以插件形式独立**安装、启用、停用与升级**，插件之间的依赖与版本由解析器
自动对齐，全部操作无需重启服务。

## 1. 插件清单（Manifest）

每个插件必须提供一份自描述的 JSON 清单：

```json
{
  "id": "text-search",
  "version": "1.2.0",
  "entrypoint": "text_search.plugin:Plugin",
  "description": "全文检索能力",
  "capabilities": ["search"],
  "permissions": ["http.get"],
  "dependencies": {
    "tokenizer": "^1.0.0",
    "dict-zh": ">=2.1.0,<3.0.0"
  }
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | 插件标识，小写字母/数字/连字符：`^[a-z0-9][a-z0-9-]{0,63}$` |
| `version` | 是 | 语义化版本 `major.minor.patch`（可带预发布后缀） |
| `entrypoint` | 否 | 加载入口 `module:attr`；值为类时会被实例化 |
| `description` | 否 | 描述文本 |
| `capabilities` | 否 | 本插件对外暴露的能力名列表 |
| `permissions` | 否 | 调用宿主外部能力前必须在此声明 |
| `dependencies` | 否 | 依赖映射：`插件id -> 版本范围` |

### 清单校验与拒绝原因

清单缺失或非法时**整体拒绝**，不会留下加载了一半的插件（`install` 在校验通过前
不写入任何状态）。拒绝原因通过 `ManifestError.reason` 区分：

- `manifest_missing`：清单缺失或为空
- `manifest_not_json` / `manifest_not_object`：不是合法 JSON / 不是对象
- `missing_field` / `invalid_field_type`：缺必填字段 / 字段类型错误
- `invalid_plugin_id` / `invalid_version` / `invalid_version_range`
- `invalid_dependency` / `invalid_capability` / `invalid_permission`
- `self_dependency`：插件依赖自身

HTTP 层对应 `422`，响应体含 `reason` 与 `field`。

## 2. 版本解析规则

版本范围语法：逗号分隔的比较子（`>=`、`<=`、`>`、`<`、`==`、`!=`），裸版本号等价
于 `==`，`^1.2.3`（兼容到下一个主版本）、`~1.2.3`（兼容到下一个次版本）、`*`。

解析器（`plugin_runtime.resolver.resolve`）在**所有已启用插件 + 本次目标插件**
之上解出一组一致的版本：

- 对每个参与插件，从已安装版本中选择一个，使所有依赖范围同时满足；
- 已启用版本优先保持不变（稳定），否则选择满足约束的最高版本；
- 依赖的传递闭包中已安装但未启用的插件会被自动纳入解析并启用；
- **版本冲突**：整体拒绝，`ResolutionError.reason = version_conflict`，
  `details.conflicts` 指明冲突来源（谁、要求什么、实际是什么）；
- **环形依赖**：整体拒绝，`reason = circular_dependency`，`details.cycle` 给出环路径；
- **未知依赖**：`reason = unknown_dependency`，`details.missing` 指出缺失插件；
- **确定性**：相同输入必然得到相同结果（插件 id 字典序、版本降序、拓扑序均确定）。

HTTP 层解析失败返回 `409` 并携带上述 `reason` 与 `details`。

## 3. 生命周期语义

状态机：`installed → enabled → disabled`（可再 `enable`），任何状态下连续失败
达到阈值进入 `quarantined`。

- **install**：仅校验并登记清单，可重复（幂等）。
- **enable**：解析依赖并加载一组一致的插件；重复启用是幂等 no-op。
- **disable**：排空并下线目标插件及其全部（传递）依赖方；重复停用幂等。
- **upgrade**：重新解析并切到满足约束的最高版本。**先加载新版本、原子切换、
  再排空旧版本**：切换瞬间之后所有新请求都走新版本，仍在处理中的旧请求在旧
  版本上跑完后旧版本才卸载。任何时刻对外暴露的都是某个完整版本，绝不新旧混合。
- **recover**：人工修复后解除隔离。

### 并发、超时与取消

- **提交与排空分离**：所有生命周期变更的状态提交（解析 → 加载 → 原子切换 →
  旧版本开始排空）由一把异步锁串行化，且提交点内**没有任何 await**，因此超时或
  取消只会发生在提交点之前（状态未变）或之后（状态已一致），运行时任一时刻都
  处于一致状态。并发的启用/停用/升级收敛到确定结果，重复请求不会重复加载
  （加载器对每个版本至多调用一次）。
- **排空不阻塞无关插件**：等待在途请求结束（排空）发生在提交之后、锁之外。
  某个插件正在排空时，对其它插件的启用/停用/升级只需等待一次毫秒级的状态
  提交，不会被排空拖住。
- **排空是事件驱动的**：操作会等到在途请求自然结束，并在最后一个在途请求
  完成的瞬间返回——而不是干等到排空上限。`drain_timeout`（默认 30s）与
  `timeout` 参数只是"卡死请求"的安全上限，不是常规等待时间。
- **在途请求与新调用的去向**：切换提交的瞬间旧版本即停止接受新请求；仍在
  处理中的旧请求在旧版本上跑完，全部跑完后旧版本才卸载。升级期间发起的新
  调用自动落到新版本；停用提交后发起的新调用得到明确的
  `PluginStateError("... not enabled")`（HTTP `409`），不会抛出含义不明的
  中间态错误，也不会出现新旧版本同时对外服务。
- **超时与取消不留残骸**：排空等待基于回调而非阻塞线程，操作超时或被取消时
  不会遗留卡住的等待线程；已提交的状态保持一致（新版本服务/插件已停用），
  未提交的状态维持原样，任何插件都可以通过再次 `enable`/`disable`/`upgrade`
  恢复正常，不存在卡死的中间状态。
- **失败计数并发准确**：健康计数器内部加锁，多个请求同时失败时计数既不丢失
  也不重复累计；连续失败达到阈值后稳定进入隔离，隔离后的调用快速失败
  （`QuarantinedError`，HTTP `423`），原因与恢复方式可查询。

### 失败隔离

- 加载或执行中的异常按插件独立计数，互不影响宿主与其他插件。
- 连续失败达到阈值（默认 3 次）后插件被**隔离**：后续调用快速失败
  （`QuarantinedError`，HTTP `423`）。
- 隔离状态、失败原因、最近失败记录与恢复方式可通过
  `GET /plugins/{id}`（`health` 字段）查询；修复后调用
  `POST /plugins/{id}/recover` 恢复。

### 权限与审计

- 插件通过 `PluginContext.call(capability, ...)` 调用宿主能力，必须先在清单
  `permissions` 中声明。
- 未声明的调用被拒绝（`PermissionDeniedError`，HTTP `403`），且**一定**写入
  审计日志（`GET /audit`），拒绝不会被静默忽略。

## 4. HTTP 管理接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/plugins` | 安装插件（body: `{"manifest": "<json>"}`） |
| `GET` | `/plugins` | 列出全部插件及状态 |
| `GET` | `/plugins/{id}` | 查询单个插件（含健康与隔离信息） |
| `POST` | `/plugins/{id}/enable` | 启用（可带 `timeout`） |
| `POST` | `/plugins/{id}/disable` | 停用（可带 `timeout`） |
| `POST` | `/plugins/{id}/upgrade` | 升级（可带 `timeout`） |
| `POST` | `/plugins/{id}/recover` | 解除隔离 |
| `POST` | `/plugins/{id}/invoke/{cap}` | 调用插件能力 |
| `GET` | `/audit` | 查询权限审计记录（可按 `plugin_id` 过滤） |

## 5. 运行测试

```bash
uv run python -m pytest tests/ -q
```

覆盖：清单校验、版本冲突、环形依赖、解析确定性、并发启停/升级、升级排空、
超时与取消、失败隔离与恢复、权限拒绝与审计、HTTP 端到端生命周期。

### 并发与排空语义的验证方法

`tests/test_concurrency.py` 稳定复现并钉住以下场景，可作为上线前的验证清单：

- `test_disable_returns_when_in_flight_finishes_not_at_timeout` /
  `test_upgrade_returns_when_in_flight_finishes_not_at_timeout`：
  在途请求期间发起停用/升级，操作返回时间贴近在途请求真实结束时间，
  远低于 30s 的排空上限；
- `test_draining_plugin_does_not_block_unrelated_operations`：
  一个插件排空期间，无关插件的启用/停用/升级在秒级内完成；
- `test_invokes_during_upgrade_always_land_on_a_complete_version` /
  `test_invokes_during_disable_succeed_or_get_clear_rejection`：
  升级/停用期间的调用要么落到完整版本，要么得到明确的 `not enabled` 拒绝；
- `test_concurrent_failures_counted_exactly` /
  `test_concurrent_failures_quarantine_stably_and_fail_fast`：
  并发失败计数精确、隔离稳定且隔离后快速失败；
- `test_disable_timeout_leaves_consistent_recoverable_state` /
  `test_cancelled_disable_leaves_consistent_state`：
  超时与取消之后状态一致且可恢复；
- `test_concurrent_duplicate_ops_converge_without_duplicate_loads` /
  `test_high_load_storm_always_recovers_to_a_consistent_state`：
  批量并发操作收敛到一致结果，不重复加载，不留卡死状态。
