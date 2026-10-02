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

- 所有生命周期变更由一把异步锁串行化：并发的启用/停用/升级收敛到确定结果，
  重复请求不会重复加载（加载器对每个版本至多调用一次）。
- 状态变更集中在一个**无 await 的同步提交点**：超时或取消只会发生在提交点之前
  （状态未变）或之后（状态已一致），运行时任一时刻都处于一致状态。
- 操作超时（`timeout` 参数）后旧实例被强制卸载，不会停留在中间状态。

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
