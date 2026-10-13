# CampusReserve

面向课程设计的校园羽毛球场高并发预约 MVP。目标场景为 10,000 人同时抢订、峰值
5,000 RPS，并通过 Redis 原子扣减与数据库唯一约束实现 0 超卖。当前先提供 PostgreSQL
同步预约原型作为数据库一致性基线，正式预约入口已经切换为 Redis Lua 原子占位和
Redis Streams 异步落库。Booking Worker 消费占位事件并幂等写入 PostgreSQL，页面通过
结果查询接口等待最终确认。

现场启动、功能演示和压测操作见[答辩操作手册](docs/答辩操作手册.md)；逐文件说明和现场修改定位见[项目代码分析与现场修改指南](docs/项目代码分析与现场修改指南.md)。

## 当前工程底座

- Vue 3 + TypeScript 前端，由 Nginx 提供静态资源
- Nginx 反向代理及 `least_conn` 双 API 实例负载均衡
- Python 3.12 + FastAPI API
- PostgreSQL 16 + SQLAlchemy + Alembic
- Redis 7（AOF 持久化）+ Redis Streams 双预约 Worker
- APScheduler 独立调度进程
- Prometheus 双实例指标采集 + Grafana 数据源自动配置
- Locust 压测入口
- Pytest、Ruff 与容器健康检查
- 14 张核心业务表、数据库约束和固定场馆种子数据
- Argon2 密码哈希、30 分钟 JWT，以及学生和管理员页面
- 定时生成明天的 210 个场地库存，并在每天 08:00 自动放号
- PostgreSQL 同步预约闭环：幂等、行锁、每日两个时段额度和数据库防超卖
- Redis Lua 原子占位：库存、每日额度、请求幂等和事件入流一次完成
- Booking Worker：消费 `reservation-events`、数据库幂等落库、故障消息重领和死信处理
- 管理闭环：场馆与场地维护、模板及例外日期、库存预览与放号控制、预约检索、现场签到、审计及完成状态归档

后端按 MVC 分层组织：`controllers` 接收 HTTP 请求，`services` 承载业务逻辑，后续
各业务域的 `models`/`repositories` 负责数据模型和持久化。API、异步 Worker 和调度器
共享 `src/campus_reserve` 中的业务层，不在入口程序中堆放业务规则。

数据库模型、模拟登录和库存生成阶段已完成。当前迁移版本为 `0006_next_day_parallel`，包含用户、每日额度、
场馆、场地、开放模板、例外日、放号批次、库存时段、预约请求、预约、预约明细、状态
历史、签到和审计日志。数据库会直接拒绝每日超过两个时段、库存重复占用、
非法状态组合和非 60 分钟库存。

完整需求和开发计划见[项目规划](docs/project-plan.md)。

## 一键启动

需要 Docker Desktop 已启动。在项目根目录执行：

```powershell
Copy-Item .env.example .env
docker compose up -d --build
docker compose ps
```

`.env` 仅用于本地课程设计；如需提交仓库或部署到其他机器，应修改其中的数据库密码和
`JWT_SECRET`。即使暂不复制 `.env`，Compose 也带有可运行的本地默认值。

## 本地入口

| 组件 | 地址或端口 | 说明 |
|---|---|---|
| MVP 页面 | <http://localhost:8080> | 由 Nginx 提供并代理 API |
| 登录接口 | <http://localhost:8080/api/v1/auth/login> | 模拟登录并签发 JWT |
| 当前用户 | <http://localhost:8080/api/v1/auth/me> | 使用 Bearer JWT 获取用户资料 |
| 场馆列表 | <http://localhost:8080/api/v1/venues> | 使用 Bearer JWT 查询唯一场馆和 15 片场地 |
| 库存查询 | <http://localhost:8080/api/v1/venues/1/inventory> | 默认查询明天的 210 个时段库存，也可指定 `service_date` |
| 提交预约 | `POST /api/v1/booking-requests` | Redis 原子占位，返回 `202 PROCESSING` 或最终结果 |
| 预约结果 | `GET /api/v1/booking-requests/{request_id}` | 查询 `PROCESSING/CONFIRMED/FAILED` 最终状态 |
| 我的预约 | <http://localhost:8080/api/v1/bookings> | 查询当前学生的预约记录 |
| 管理台 | <http://localhost:8080> | 使用管理员账号登录后自动进入管理页面 |
| 管理 API | `GET /api/v1/admin/overview` | 其余管理接口见 API 文档 |
| API 健康状态 | <http://localhost:8080/api/v1/system/health> | 同时检查 PostgreSQL、Redis |
| API 文档 | <http://localhost:8000/docs> | 直连 `api-1`，便于本地调试 |
| Locust | <http://localhost:8089> | 压测控制台 |
| Prometheus | <http://localhost:9090> | 已采集两个 API 实例 |
| Grafana | <http://localhost:3000> | 本地账号 `admin` / `admin` |
| PostgreSQL | `localhost:5432` | 数据库 `campus_reserve` |
| Redis | `localhost:6379` | 默认 DB 0 |

学生首页直接显示“今天”和“明天”两个使用日期，默认显示明天；每天 08:00 放出明天的库存。
两个日期中尚未开始的可用时段可以预约，同一学生同一时间可订不同场地，每天累计最多两个时段。
调度器会补齐当天尚不存在的库存批次；今天显示全部时段，已开始且被预约的显示“已预约”，
其他已开始的时段显示“已过期”，尚未开始的空闲时段仍可预约。

## 模拟登录账号

课程演示时可直接在首页使用以下账号，数据库中仅保存 Argon2 密码哈希：

| 用户名 | 密码 | 身份 |
|---|---|---|
| `student001` | `Student@123` | 演示学生一 |
| `student002` | `Student@123` | 演示学生二 |
| `venue_admin` | `Admin@123` | 场馆管理员 |
| `platform_admin` | `Admin@123` | 平台管理员，可维护开放模板 |

登录成功后前端把短期 JWT 保存在当前浏览器标签页的 `sessionStorage`，关闭标签页即清除；
JWT 默认 30 分钟失效。登录接口另有限速，预约压测流量不会与密码哈希计算共用限流额度。

## 管理操作顺序

管理员登录后可按“场馆与场地 → 开放规则 → 库存与放号 → 预约与签到 → 审计日志”操作。
库存先预览、生成草稿并发布，达到放号时间后可手工放号；已开放批次可暂停、恢复或关闭。
暂停及关闭会同步拦截 Redis 预约入口，恢复时会核对数据库库存与未完成占位。
场馆或场地停用会暂停当前开放批次，恢复运营后需手工恢复批次。
预约查询支持编号、用户名、日期和场地筛选；现场签到接口可安全重试。
调度器每分钟将结束时段的有效预约归档为 `COMPLETED`。

## 验证与日常命令

```powershell
# 查看所有容器状态
docker compose ps -a

# 查看服务日志
docker compose logs -f api-1 api-2 nginx booking-worker-1 booking-worker-2

# 执行单元测试和静态检查
docker compose exec -T api-1 pytest
docker compose exec -T api-1 ruff check apps src tests migrations

# 只运行 Redis Lua 独立原型测试（不经过 HTTP 和前端）
docker compose exec -T api-1 pytest tests/integration/test_redis_atomic_reservation.py

# 停止服务，保留数据库和监控数据
docker compose down
```

如确实需要清空本地演示数据，可执行 `docker compose down -v`。该命令会删除本项目的
PostgreSQL、Redis、Prometheus 和 Grafana Docker 卷，不能用于保留数据的环境。

## Redis Lua 独立占位原型

原型采用带 `{booking}` hash tag 的 Redis 键，保证一次 Lua 调用涉及的库存、用户额度、
请求结果和事件流位于同一 Redis Cluster 槽位。抢占成功后返回
`RESERVED`，并向独立的 `reservation-events` Stream 写入一条待落库事件；失败返回稳定
错误码并缓存请求结果，重复请求不会再次扣减库存或重复写入事件。

当前脚本覆盖以下规则：

- 每次必须选择 1～2 个不同的场地时段
- 每名学生每天最多占用 2 个场地时段
- 同一学生可以预约同一时间的不同场地，分别计入每日额度
- 一个库存时段只能由一个请求占用
- 同一个请求 UUID 和相同内容可安全重试
- 同一个请求 UUID 携带不同内容会返回 `IDEMPOTENCY_CONFLICT`

Redis 键均设置过期时间，测试使用随机 hash tag 隔离数据并在结束后清理，不会占用正式
库存键。调度器会在数据库库存放号后幂等初始化 Redis，API 请求携带接口返回的
`release_batch_id` 和 `service_date`，Lua 会先核对批次日期再执行占位。

## Booking Worker 异步落库

两个 Worker 使用同一个 Redis Streams 消费组读取 `reservation-events`。每条消息会重新
经过数据库事务、行锁和唯一约束校验，再写入预约请求、预约主记录、预约明细、每日额度
和状态历史。数据库提交成功后，Worker 才更新 Redis 请求结果并执行 `XACK`。

如果进程在数据库提交后、`XACK` 前退出，消息会在空闲 30 秒后被另一个 Worker 通过
`XAUTOCLAIM` 重领；请求 UUID 和数据库唯一约束保证重放不会产生第二笔预约。格式错误或
指纹不一致的消息会写入 `reservation-events:dead-letter`。处理异常默认最多投递 5 次
（可通过 `BOOKING_MAX_DELIVERIES` 调整）；达到上限后先持有请求级数据库锁核对结果：
已提交的预约恢复为成功，未提交的请求记录失败并释放仍属于它的 Redis 占位，然后写入死信流并确认。
数据库不可查询时保留待处理消息，避免在提交结果不明时释放库存。

如果数据库明确拒绝落库，Worker 会通过补偿 Lua 仅释放仍属于该请求的临时占位，并回退
该用户的 Redis 每日额度；补偿本身同样幂等，不用于用户主动取消预约。

Redis 请求结果 Hash 会增加 `persistence_status`、`booking_id`、`booking_no`、
`persistence_error_code` 和 `persisted_at` 字段，供后续结果查询 API 使用。

## 每名学生每天两个时段的并发额度

Redis 的用户额度值同时包含 PostgreSQL 已确认时段和 Streams 中等待落库的临时时段。
Scheduler 在 Redis 库存首次生成或缓存恢复时，会把数据库中已确认的用量与场地库存放在
同一次 Lua 调用中原子初始化；新请求占位后立即增加临时额度，明确落库失败才由补偿 Lua
回退，因此并发请求不会绕过两个时段的额度限制。

Worker 落库时按“学生 + 预约日期”取得 PostgreSQL 事务级 advisory lock，再对
`user_daily_usage` 执行 `SELECT ... FOR UPDATE`。数据库会重新统计并校验累计时段，且
`used_slot_count BETWEEN 0 AND 2` 检查约束提供最后一道保护。一次选择的两个时段
位于同一事务中，要么同时确认，要么均不占用库存。

集成测试会并发提交同一学生的 100 个单时段请求，并断言 PostgreSQL 最终只有 2 个确认、
98 个失败、每日用量恒为 2；同时覆盖“数据库已确认 1 个时段 + Redis 待确认 1 个时段”的
恢复场景以及双时段全成全败。

## 下一步开发顺序

1. 为异步预约链路补充 Prometheus 指标和管理端死信查看入口。
2. 更新 Locust 脚本，执行 10,000 用户、5,000 RPS 压测及调优。
3. 完成课程设计报告中的测试数据、时序图和零超卖结果分析。
