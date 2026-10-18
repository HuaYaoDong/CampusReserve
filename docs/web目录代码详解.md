# `web` 目录代码详解

> 本文对应当前 Vue 3 + TypeScript 前端代码，适合答辩前阅读和现场定位。后端业务实现见 [`src` 目录代码详解](src目录代码详解.md)，三个服务入口见 [`apps` 目录三个进程入口详解](apps目录三个进程入口详解.md)。

## 1. 前端怎样启动

```text
web/index.html
  └─ 加载 web/src/main.ts
       ├─ 引入全局样式 web/src/style.css
       └─ 挂载 web/src/App.vue
            ├─ 未登录：显示登录页
            ├─ 学生：显示库存、预约请求和我的预约
            └─ 管理员：显示 web/src/AdminPanel.vue
```

当前项目没有单独的前端路由文件；根组件 [`App.vue`](../web/src/App.vue) 根据登录状态和用户角色决定显示哪一部分。页面通过 `/api/v1/...` 请求后端，开发环境由 Vite 代理 `/api`，容器部署时由 Nginx 代理。

## 2. `web` 根目录文件

| 文件 | 作用与通常修改场景 |
| --- | --- |
| [`index.html`](../web/index.html) | 浏览器加载的 HTML 外壳，包含 `#app` 挂载点并引入 `/src/main.ts`；修改网页入口元素时看它。 |
| [`package.json`](../web/package.json) | 声明 Vue、TypeScript、Vite、Pinia 等依赖及 `dev`、`build`、`preview` 命令；新增前端依赖或命令时修改。 |
| [`tsconfig.json`](../web/tsconfig.json) | TypeScript 编译和严格检查设置，覆盖 `src` 代码与 Vite 配置；调整类型检查规则时看它。 |
| [`vite.config.ts`](../web/vite.config.ts) | 启用 Vue 插件；开发服务器使用 5173 端口，并将 `/api` 转发到 `http://localhost:8000`。只影响 Vite 开发环境，容器访问的代理见 `deploy/nginx/default.conf`。 |

## 3. `web/src` 文件总览

| 文件 | 主要作用 |
| --- | --- |
| [`main.ts`](../web/src/main.ts) | 创建 Vue 应用，注册 Pinia、引入全局 CSS，并挂载根组件。当前可见的页面状态主要写在组件自身。 |
| [`env.d.ts`](../web/src/env.d.ts) | 引入 Vite 客户端类型声明，使 TypeScript 理解 `import.meta.env` 等浏览器端类型。 |
| [`App.vue`](../web/src/App.vue) | 根组件和学生端：登录、恢复会话、今天/明天库存、时段选择、异步预约结果找回、“我的预约”。 |
| [`AdminPanel.vue`](../web/src/AdminPanel.vue) | 管理端：总览、场馆与场地、开放规则、库存批次、预约签到和审计。 |
| [`style.css`](../web/src/style.css) | 登录页、学生页、库存矩阵、状态颜色、请求卡片和响应式布局的全局样式。`AdminPanel.vue` 末尾还有管理端局部样式。 |

## 4. `App.vue`：登录与学生预约页面

一个 Vue 单文件组件分成三部分：`<script setup lang="ts">` 定义数据与事件；`<template>` 生成页面；样式主要由 `style.css` 提供。此文件较长，建议按下面的功能块阅读。

### 4.1 数据类型与页面状态

文件开头定义 `UserProfile`、`Inventory`、`InventorySlot`、`Booking`、`BookingRequestResult`、`TrackedRequest` 等 TypeScript 类型。它们描述前端预期收到的后端 JSON；如果后端接口增加或改名字段，前端类型与使用位置都要同步检查。

主要状态包括：

| 状态 | 用途 |
| --- | --- |
| `token`、`user` | 当前登录凭证与用户；决定显示学生页还是管理页。 |
| `todayServiceDate`、`requestedServiceDate` | 上海时区的今天，以及当前正在看的场地使用日期。 |
| `inventory`、`selectedSlotIds` | 当前日期的场地库存和已选择的时段。 |
| `trackedRequests` | 浏览器保存的预约请求记录，包括请求 UUID、状态与错误信息。 |
| `bookings` | 服务端已确认的个人预约列表。 |
| `health`、`healthError` | API、PostgreSQL、Redis 的健康状态展示。 |

`shanghaiDateISO()` 明确以 `Asia/Shanghai` 计算今天；`nextDateISO()` 计算明天。`selectableServiceDates` 限制页面只选择**今天和明天**。页面默认先看明天，用户可切换今天。真正能否提交，还要由服务端核验日期、批次和时段状态。

### 4.2 登录与会话

- `login()` 调用 `/api/v1/auth/login`，成功后保存令牌、用户资料；学生随后加载库存、个人预约和待处理请求。
- `restoreSession()` 刷新页面时用现有令牌请求 `/api/v1/auth/me`，恢复用户状态；令牌失效则退出。
- `logout()` 清理当前页面数据及 `sessionStorage` 中的令牌，避免上一用户的信息留在页面。
- 模板根据 `restoring`、是否登录、用户角色显示加载页、登录页、学生页或 `AdminPanel.vue`。

令牌放在 `sessionStorage`，关闭浏览器会话后通常不再保留。管理员页面由 `App.vue` 传入令牌和用户对象；管理员组件的退出事件仍交由 `App.vue` 处理。

### 4.3 库存日期与状态显示

`refreshInventory()` 请求 `/api/v1/venues/1/inventory?service_date=...`。这里的场馆 ID 当前写为 `1`，与项目的单场馆演示配置对应。代码用 `inventoryFetchRevision` 只接受最后一次日期查询的响应，避免快速切换日期时旧响应覆盖新页面。

`selectServiceDate()` 切换今天/明天并清空旧选择；`updateDateWindow()` 在上海时区跨午夜时更新日期窗口，也会清除已不再可选的时段。`bookableSlotCount` 计算当前可预约数。

`visibleSlotStatus()` 负责一个容易被问到的显示规则：今天已开始的时段仍显示；若原状态已订出，显示**已预约**，其余显示**已过期**。`slotLabel()` 把状态码转成中文。后端库存接口也会计算状态，前端这里再按当前浏览器时间处理展示，因此修改状态规则时应同时检查两端。

### 4.4 选择与提交预约

`toggleSlot()` 只让用户选可预约时段，一次最多选 2 个；选择改变时创建新的 `request_id`。页面允许同一时间选择两片不同场地，额度按时段数量计算。

`submitBooking()` 的关键顺序是：

1. 再次检查日期、库存、选择的时段和登录状态。
2. 保存 `request_id`、批次 ID、使用日期和时段信息到 `trackedRequests`，**先写本地记录，再发送 HTTP 请求**。
3. 调用 `POST /api/v1/booking-requests`；请求体包含 `request_id`、`release_batch_id`、`service_date`、`slot_ids`。
4. 若返回 `PROCESSING`，调用 `waitForBookingResult()` 短时轮询结果；约 5 秒后仍无最终结果，就保留请求编号供用户以后继续查询。
5. 成功后刷新库存和“我的预约”；失败则展示服务端错误或“结果暂未确认”的提示。

`request_id` 是查询凭证，`booking_no` 是数据库确认后的正式预约编号。页面不能仅凭请求已发出或 Redis 预占成功就显示预约成功。

### 4.5 请求找回与“我的预约”

`saveTrackedRequests()` 将请求记录按用户 ID 存入 `localStorage`：保留全部处理中请求，已结束记录最多保留 30 条。`loadTrackedRequests()` 登录后恢复这些记录，并检查保存内容的基本形状。

`queryTrackedRequest()` 用请求 UUID 调用 `GET /api/v1/booking-requests/{request_id}`；确认成功后刷新个人预约和库存。`refreshProcessingRequests()` 分批查询尚在处理的请求；`removeTrackedRequest()` 只移除浏览器中的已结束记录，不会删除服务端预约。`refreshBookings()` 调用 `GET /api/v1/bookings`，展示已落库的正式预约，还会用这些预约修正本地请求状态。

**记忆点：** `localStorage` 只是帮助找回请求编号，预约是否成立始终以 Redis/Worker/数据库的最终结果为准。

### 4.6 页面模板在哪里

| 页面部分 | 对应内容 |
| --- | --- |
| 登录页 | 品牌介绍、账号密码输入、演示账号、服务状态。 |
| 顶部与概览 | 用户信息、场馆、每日额度、使用日期、系统状态。 |
| 库存矩阵 | 今天/明天切换、场地行和时段列、各类状态按钮、已选择时段和确认预约。 |
| 预约请求记录 | `PROCESSING/CONFIRMED/FAILED` 卡片、继续查询、移除本地记录。 |
| 我的预约 | 正式预约编号、使用日期、场地和时段、预约状态。 |

## 5. `AdminPanel.vue`：管理端页面

`AdminPanel.vue` 从父组件接收 `token`、`user`，管理员令牌失效时发出 `logout` 事件。它通过统一的 `api<T>()` 函数请求 `/api/v1/admin/...`，统一附加 Bearer Token、解析业务错误；`run()` 统一处理忙碌状态、成功提示和错误提示。

### 5.1 页面状态与六个栏目

| 栏目 | 页面与对应方法 |
| --- | --- |
| 管理总览 | `loadOverview()` 获取场馆、场地、模板、例外日期和批次；页面展示统计与概况。 |
| 场馆与场地 | `saveVenue()`、`saveCourt()` 更新名称、公告、场地状态和设施 JSON。 |
| 开放规则 | `saveTemplate()`、`saveTemplateEdit()`、`setTemplateStatus()` 管理模板；`saveException()` 设置全馆或单片场地的例外日期。 |
| 库存与放号 | `generate()` 预览或生成日期库存；`batchAction()` 发布、放号、暂停、恢复、关闭批次，也可重新同步开放批次库存。 |
| 预约与签到 | `searchBookings()` 按预约编号、账号、日期、场地查找；`checkIn()` 为已确认预约办理签到。 |
| 审计日志 | `loadAudits()` 查询管理员操作记录。 |

`isPlatform` 用于平台管理员专属控件的页面显隐。**前端显隐不等于权限控制**：真正能否操作由后端 `admin/controllers/http.py` 和 `admin/services.py` 判断。

### 5.2 一次管理操作的读法

以“暂停某日批次”为例：模板按钮调用 `batchAction(batch, 'pause')` → 统一 `api()` 向管理接口发 POST → 后端管理服务改变批次并更新 Redis 门控 → `loadOverview()` 重新取数据 → 页面显示最新状态。因此批次按钮改变的是实际预约入口，不只是表格文字。

库存生成是另一个典型流程：先用 `generate(true)` **预览但不写库**，确认数量后用 `generate(false)` 创建草稿，再根据状态发布和放号。既有批次可能无法像未生成日期那样重新修改规则，应先看后端限制。

## 6. 其余 `web/src` 文件

### `main.ts`

[`main.ts`](../web/src/main.ts) 调用 `createApp(App).use(createPinia()).mount('#app')`。它只负责应用装配，不写登录或预约规则。目前 Pinia 已注册，但主要页面状态仍由两个 Vue 组件的 `ref`、`reactive` 和 `computed` 管理。

### `env.d.ts`

[`env.d.ts`](../web/src/env.d.ts) 引用 `vite/client` 类型。若要给 `import.meta.env` 增加自定义变量的类型，可在此扩展；配置真实变量值仍需检查构建环境。

### `style.css`

[`style.css`](../web/src/style.css) 定义全局颜色、字体、输入框、按钮、登录页、学生仪表盘、库存矩阵、状态色、请求卡片和我的预约卡片。`.slot-cell.available/.processing/.booked/.expired` 等类对应时段状态。库存表格保留横向滚动，避免窄屏下场地名与小时列挤压；`920px` 和 `560px` 两档媒体查询处理平板、手机布局。管理端特有样式还在 `AdminPanel.vue` 的 `<style scoped>` 中。

## 7. 常见现场修改定位

| 老师提出的变更 | 首先修改 | 同时核对 |
| --- | --- | --- |
| 改标题、提示文案或登录页内容 | `App.vue` 的 `<template>` | 如果涉及业务规则，再核对后端实际规则 |
| 改颜色、卡片、手机布局 | `style.css`；管理端专有样式查 `AdminPanel.vue` | `@media` 断点和状态类 |
| 改学生默认日期或可选日期 | `App.vue` 的 `todayServiceDate`、`selectableServiceDates`、`selectServiceDate()` | Scheduler、库存接口、预约服务对日期的校验 |
| 改时段显示“已预约/已过期” | `App.vue` 的 `visibleSlotStatus()`、`slotLabel()` | 后端 `inventory/controllers/http.py` 的返回状态 |
| 改每天最多可选几个时段 | `App.vue` 的 `toggleSlot()` | Redis Lua、数据库预约服务、表约束和测试 |
| 改提交预约后的等待提示 | `App.vue` 的 `submitBooking()`、`waitForBookingResult()` | 请求记录与继续查询入口 |
| 改请求找回的保存方式 | `App.vue` 的 `saveTrackedRequests()`、`loadTrackedRequests()` | 用户隔离、请求 UUID、隐私和容量 |
| 改管理员某个操作 | `AdminPanel.vue` 对应的 `save*()`、`generate()` 或 `batchAction()` | 后端管理接口、角色、规则和审计 |
| 改开发端口或接口代理 | `vite.config.ts` | 容器部署时的 Nginx 配置是另一套代理 |

**现场修改顺序：**先确定是页面展示、接口字段，还是业务规则；再按“组件 → HTTP 接口 → 服务 → Redis/数据库”追踪。前端的选项限制和按钮禁用只负责用户体验，服务端仍需独立校验。

## 8. 本地构建与验证

在项目根目录的 PowerShell 中执行：

```powershell
cd C:\Code\Python\CampusReserve\web
npm install
npm run build
```

`npm run build` 可检查 TypeScript/Vite 构建是否通过。若使用 Docker Compose 提供的前端页面，修改源码后需要重新构建并启动 Nginx 镜像：

```powershell
cd C:\Code\Python\CampusReserve
docker compose up -d --build nginx
```

## 9. 一分钟总结

> `web` 是 Vue 前端。`main.ts` 把应用挂到浏览器页面；`App.vue` 负责登录、学生库存和预约全过程；`AdminPanel.vue` 负责管理闭环；`style.css` 负责主要视觉布局。前端保存请求 UUID 用于异步结果找回，但正式预约、额度与权限都由后端确认。
