# AI Checkin Hub

聚合多个 AI 中转 / 公益站点的签到与额度监控容器。一个 8888 端口的控制台，管住所有站的「今天签了没、还剩多少额度、有哪些模型能用」。

## 它解决什么问题

手里同时有多个 OpenAI 兼容中转站时，常见麻烦是：每个站各有签到入口、有的站被 WAF 挡、有的站只支持 linux.do 登录、余额和可用模型散落各处。本项目把这些收敛成一套统一的数据模型和调度器：

- **自动签到**：固定时间或随机窗口，确定性哈希选点，容器重启不重复签、不漏签
- **额度看板**：按各站 `quota_per_unit` 统一折算成美元，跨站汇总
- **模型目录**：拉取公开定价（输入/输出每百万 token）与当前 Key 真实可用的模型
- **双通道执行**：普通站走 HTTP 直连；被 Cloudflare / 阿里云 WAF 拦的站走真实 Chromium
- **凭据加密**：密码、Token、Cookie 一律 Fernet 加密落库，接口只返回掩码
- **通知**：签到结果推送到企业微信 / 钉钉 / 飞书 / ntfy / 任意 JSON Webhook

## 对目标站点的实测结论

开工前对本项目涉及的站点做过探测，结果决定了架构必须做双通道：

| 站点 | `/api/status` | 框架 | 直连可行性 |
|---|---|---|---|
| api.hcnsec.cn | 200 | New API | 可直连 |
| ultrarouter.org | 200 | New API | 可直连 |
| new.xinjianya.top | 200 | New API | 可直连 |
| api.justwoker.icu | 200 | New API | 可直连 |
| seekai.cc | 403 | 未知 | 需浏览器通道 |
| kktoken.cc | 403 | 未知 | 需浏览器通道 |
| ps.air-outer.com | 200(HTML 挑战) | 未知 | 需浏览器通道 |
| api.kunyou.asia | 404 | 非 New API | 仅 OpenAI 兼容面，签到需自定义端点 |
| anyrouter.top | TLS 失败 | 未知 | 网络出口相关，需实际环境复测 |

> `api.hcnsec.cn` 的探测结果为例：`framework=newapi`、`system_name=新疆幻城网安科技公益大模型安全网关`、`quota_per_unit=500000`，即 50 万 quota = 1 美元。

New API 系的鉴权中间件在路由前统一返回 401，因此**无法用状态码判断签到端点是否存在**。本项目只对只读端点做自动探测，签到端点以配置为准，由「立即签到」实测确认，避免探测动作误触发真实签到。

## 架构

```
                    ┌──────────────────────────────┐
   Browser ────────▶│ FastAPI (8888) 单页控制台     │
                    │  · 会话 HMAC Cookie           │
                    │  · REST API                   │
                    └───────────┬──────────────────┘
                                │
              ┌─────────────────┼──────────────────┐
              ▼                 ▼                  ▼
      ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
      │ 调度器 tick   │  │ 适配层        │  │ SQLite /data │
      │ 每分钟一次    │─▶│ 直连 / 浏览器 │─▶│ 日志·额度·配置│
      │ 哈希定时刻    │  │ 四种认证      │  │ WAL 模式      │
      └──────────────┘  └──────┬───────┘  └──────────────┘
                               │
                    ┌──────────┴──────────┐
                    ▼                     ▼
              httpx 直连            Playwright Chromium
              (New API 系)          (WAF 站点 / OAuth)
```

**调度为什么用 tick 而不是给每站注册 cron job**：随机窗口需要每天重算，动态增删 job 容易泄漏；容器重启后内存 job 全丢，容易漏签或重复签。tick 模型把「今天该不该签、几点签」变成纯函数（站点配置 + 日期 → 时刻），重启后结果完全一致，已签状态以 `last_checkin_date` 为准，天然幂等。

**浏览器通道为什么不是模拟点击**：站点前端随时改版，DOM 选择器极易失效。这里用真实 Chromium 先通过 JS 挑战建立可信会话，再在页面上下文内 `fetch` 调用站点自己的 JSON 接口——接口契约比 DOM 结构稳定得多。

## 部署

```bash
git clone https://github.com/wdw6733/ai-checkin-hub.git
cd ai-checkin-hub
cp .env.example .env
# 编辑 .env，把 WEB_PASSWORD 改掉
docker compose up -d
```

打开 `http://<服务器IP>:8888`，用 `WEB_PASSWORD` 登录。数据全部落在 `./data`：`app.db`（SQLite）、`secret.key`（加密密钥）、`browser/`（浏览器会话）。**备份就备份这一个目录**。

自行构建：

```bash
docker compose up -d --build
```

## 添加站点

点「+ 添加站点」，填名称与 Base URL（`https://xxx/v1` 或 `https://xxx` 都可以，会自动规范化），选认证方式：

| 认证方式 | 填什么 | 适用 |
|---|---|---|
| **API Key / 访问令牌** | `sk-...`（站点「设置 → 令牌」里生成的系统访问令牌，不是模型调用 Key 也可以，只要 `/api/user/self` 认） | 最稳，优先推荐 |
| **账号密码** | 用户名 + 密码 | 站点未开验证码时可用 |
| **浏览器 Cookie** | `session=xxx; other=yyy` | 万能兜底，任何站都能用 |
| **LinuxDO 授权（自动续期）** | 不用填密钥；在账号「编辑 CK」里导入 cookie 或点「浏览器授权登录」 | **站点只开放 LinuxDO 登录时用这个** |
| **linux.do OAuth（手动 `_t`）** | linux.do 的 `_t` Cookie 值 | 兼容旧配置，已不推荐 |

保存后会自动探测框架并写回。**被 WAF 挡的站必须勾选「启用浏览器通道」**，否则请求会被挑战页拦下。

三种凭据的获取方式：

1. 登录站点 → F12 → Network → 任一 `/api/` 请求 → 复制 `Authorization: Bearer xxx` 或完整 Cookie 头
2. 若站点有「系统访问令牌」页面，优先用那个
3. LinuxDO 授权站点见下一节，不需要手动找 `_t`

## 只开放 LinuxDO 登录的站点

很多 New API 分支关掉了账号密码，只留 `Continue with LinuxDO`。这类站点拿不到
`_t` 是正常的——linux.do 的会话本来就不止一个 cookie，而站点需要的是**授权后它自己的
session**，不是 linux.do 的某个值。

项目按 checkin-panel 的思路处理：**一个账号一个持久化 Chromium profile**，授权一次后
IdP 会话留在 profile 里，之后每天由无头浏览器复用该会话重新走一遍 OAuth，换取新的站点
会话。两条入口都在站点卡片的「编辑」→ 账号行「编辑CK」里：

| 入口 | 前提 | 用途 |
|---|---|---|
| **浏览器授权登录** | 服务端有显示环境（本机 / 带桌面的机器） | 打开可见窗口，手动点一次「Continue with LinuxDO」完成授权 |
| **导入 linux.do Cookie** | 无 | 用 cookie 扩展导出 linux.do 的 cookie **JSON**，粘贴导入；导入后会当场换一次站点会话并验证 |

服务器（Docker、无桌面）只能用第二条：在电脑浏览器登录 linux.do，用 Cookie-Editor 之类的
扩展「Export」出 JSON，粘贴进来即可。**不要只复制 `_t` 的值**——导入整段 JSON 才能让
profile 具备完整会话。

授权成功后账号的认证方式会变成「LinuxDO 授权（自动续期）」。站点会话过期时，调度器会
自动用 profile 里的 linux.do 会话重新授权一次再签到，失败才记错误日志；只有 linux.do
自己的会话也失效时，才需要重新导入一次 cookie。

## 调度

- **随机窗口**：在 `窗口开始 ~ 窗口结束` 内用 `sha256(日期|站点|账号)` 取点，同一账号每天时刻不同且重启不变，天然避开整点风控
- **固定时间**：`每日 08:30`，可叠加 `±N 分钟` 抖动
- **幂等保证**：`last_checkin_date` 等于站点时区的今天时直接跳过，不会重复请求
- **补签**：容器在窗口后才启动时，当天仍会补执行一次

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/login` | 登录，返回会话 Cookie |
| GET | `/api/overview` | 汇总指标 + 今日计划 + 14 日收益 |
| GET/POST | `/api/sites` | 站点列表 / 新建 |
| PATCH/DELETE | `/api/sites/{id}` | 修改 / 删除（级联账号） |
| POST | `/api/sites/{id}/probe` | 探测框架、WAF、公开定价 |
| GET | `/api/sites/{id}/models` | 定价与可用模型 |
| POST | `/api/sites/{id}/accounts` | 添加账号 |
| POST | `/api/accounts/{id}/checkin` | 立即签到 |
| POST | `/api/accounts/{id}/refresh` | 刷新余额与模型数 |
| POST | `/api/accounts/{id}/browser-login` | 走一遍 LinuxDO 授权，保存站点会话（`headless=false` 开可见窗口） |
| POST | `/api/accounts/{id}/idp-cookies` | 导入 linux.do cookie JSON，并验证能否换到站点会话 |
| POST | `/api/checkin/all` | 全量签到（并发 3） |
| GET | `/api/logs` | 签到日志 |
| GET/PUT | `/api/settings` | Webhook 与每日刷新开关 |
| GET | `/healthz` | 容器健康检查 |

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `WEB_PASSWORD` | `admin` | 控制台密码，**必须改** |
| `SECRET_KEY` | 自动生成 | Fernet 密钥，固定它可让备份在异地解密 |
| `DATA_DIR` | `/data` | 持久化目录 |
| `MAX_CONCURRENCY` | `4` | 并发签到上限 |
| `TICK_SECONDS` | `60` | 调度 tick 间隔 |
| `HEADLESS` | `true` | 浏览器通道是否无头 |
| `HTTP_TIMEOUT` | `25` | 单请求超时（秒） |
| `VERIFY_SSL` | `false` | 是否校验站点证书 |

## 风险与边界

- **自动化签到可能违反站点使用条款**。部分站点明确禁止脚本签到，被判定为异常行为可能限制额度或封号。是否使用、用哪些站，请自行判断并承担后果。
- **本工具不绕过登录**：所有操作都使用你自己账号的合法凭据，不破解验证码、不伪造身份。
- **linux.do OAuth 模式是半自动**：Discourse 首次授权会弹出确认页，程序会尝试自动点击「授权/Authorize」，若站点改版或存在二次验证则会失败，此时请退回 Cookie 模式。
- **余额汇率是近似值**：按站点公开的 `quota_per_unit` 折算，站点若自定义倍率或分组定价，实际扣费可能不同。
- **不要把这套东西部署在公网无防护环境**：至少改掉默认密码，最好只在内网或加一层反代鉴权。

## License

MIT
