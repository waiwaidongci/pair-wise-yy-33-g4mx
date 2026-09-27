# 电网事故应急与恢复调度系统

标准库 Python 3.11+ + SQLite。系统管理停运事故、重要用户、备用容量、恢复步骤及安全依赖；接受现场离线报告并区分已合并、版本冲突和受保护记录，异常遥测单独隔离。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

默认端口 `8215`。身份使用 `X-Actor` 和 `X-Role`，角色为 `dispatcher`、`operator`、`field`。可用 `--port`、`--db` 覆盖。

## 主要接口

- `POST /api/assets`、`POST /api/facilities`：登记线路资产和医院等重要用户。
- `POST /api/outages`：创建或幂等接收同一事故。
- `POST /api/telemetry`：记录并隔离错误遥测。
- `POST /api/plans`、`/submit`、`/approve`、`/activate`：创建、提交、审批并启用安全恢复计划。
- `POST /api/plans/{id}/change`：对执行中的计划发起变更，仅创建**待接替**草稿（记录 `replaces_plan_id`）。旧版本在接替前保持当前，继续接收现场报告并允许确认步骤；同一计划只允许一个待接替版本。草稿经提交、审批后由 `/activate` 一次切换：旧版本转为已废止，内容未改动步骤的确认/受阻记录带到新版本，改动或新增步骤不带。
- `POST /api/field-reports`：合并现场离线报告，重复客户端编号不会重复写入。报告发给已废止版本记为冲突（版本已废止），发给待接替等未启用版本同样记冲突。
- `POST /api/plans/{id}/confirm`：调度员确认当前执行版本的步骤，依赖未满足时拒绝。
- `POST /api/status`：只接受当前启用（active）版本发布；已废止或未启用版本返回 409。
- `GET /api/plans/{id}`、`GET /api/state`、`GET /api/health`：详情、状态和健康检查。计划对象含 `lifecycle`/`lifecycle_label`：`current`（当前）、`pending_succession`（待接替）、`superseded`（已废止）、`draft`（普通草稿），首页以彩色徽章展示。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

当前为原型：容量和依赖是静态安全模型，不包含潮流计算、SCADA/EMS 协议、实时遥测质量码或生产级多实例锁；离线合并通过客户端编号和计划版本完成。
