# 公共健身资产问效

服务用于连接公益资金、健身设施、使用情况和维护责任，为后续建设决策提供可核验依据。

当前模块提供**健身指导内容治理后端**：把每个二维码与设施型号、安装位置、适用人群、禁忌条件和动作版本绑定，经三岗审核后发布，并对设施迁移、部件更换、风险事件、专家撤回、边缘同步与匿名反馈提供可核对的处理规则。

运行 `python3 service.py --check` 可核对服务配置；执行 `python3 service.py --port 8000 [--store data/state.json]` 后访问 `/health` 确认服务身份。加 `--store` 时状态以原子 JSON 快照持久化，缺省仅驻留内存。

## 治理规则

1. **绑定**：二维码一对一指向设施（型号 + 位置）；指导版本创建时冻结动作步骤、适用人群（`suitable_groups`）、禁忌条件（`contraindications`），缺一项即拒绝。
2. **三岗门禁**：版本提交后须依次完成运动医学审核 → 无障碍复核 → 运营批准，全部通过才能发布；任一岗要求修改即阻断后续岗位与发布。
3. **发布留痕**：发布是服务端状态迁移。同一二维码再发新版本时，旧发布置为 `superseded` 并记录 `replace_reason`；历史发布（生效/被替换/已暂停）永久可查。
4. **定向暂停 + 替代提示**：迁移、部件更换（含换型）、风险事件、专家撤回、二维码改绑只暂停受影响范围（可按设施、版本或单个二维码圈定），自动生成面向儿童/老人/伤后恢复者的安全替代提示，不波及无关内容。现场复核确认无碍后凭依据恢复；已有新版生效时旧版不能直接恢复（回退必须显式重新发布）。
5. **边缘幂等与失联恢复**：同步接口要求幂等键，重复同步返回首次结果且不产生重复事件；同步**永不**触发发布。终端恢复联网后，响应中的 `effective` 是当前有效版本的权威清单，终端必须据此覆盖本地展示决策。扫码响应与同步响应都给出 `cache_policy.max_offline_seconds`（默认 24 小时），离线超过该时长不得继续展示缓存。
6. **纠正通知可核对**：旧版本被暂停或替换时，仅向曾缓存该版本的终端下发纠正；送达分 `pending / delivered / acked` 三态，终端撤下旧缓存后回执。负责人可按通知查询每台曾缓存终端的送达与清除状态。
7. **匿名反馈防扭曲**：先领取一次性匿名令牌（不收集身份）；同一令牌重复内容拒绝，一小时内跨令牌的完全相同提交（换令牌刷）折叠并计入 `dup_count`。汇总按不同令牌计 `unique_submitters`，重复提交不扭曲结论，并可按步骤定位“看不懂/完成不了”。
8. **审计时间线**：所有状态迁移带时间、操作者、原因写入事件流，可按二维码、版本、设施过滤，回答“何时对哪些设施生效、为什么被替换、纠正是否送达”。

## 接口一览（JSON）

管理端：

- `POST /admin/facilities`、`POST /admin/qrcodes/bind`
- `POST /admin/guidance`、`POST /admin/guidance/{id}/versions`
- `POST /admin/versions/{id}/submit`、`.../reviews`（`role`/`decision`/`comment`）、`.../withdraw`
- `POST /admin/releases`（支持 `Idempotency-Key` 头）
- `POST /admin/facilities/{id}/relocate`、`.../replace-component`
- `POST /admin/risk-events`（`qr_ids` / `version_id` / `facility_id` 圈定范围）
- `POST /admin/qrs/{id}/reinstate`
- `GET /admin/releases`、`GET /admin/timeline`、`GET /admin/notices/{id}/delivery`、`GET /admin/feedback`

市民与边缘端：

- `GET /qr/{qr_id}`：当前生效内容或替代提示 + 缓存策略
- `POST /edge/terminals`、`POST /edge/sync`（幂等键 + 本地缓存清单）、`POST /edge/ack`
- `POST /feedback/token`、`POST /feedback`

## 测试

```bash
npm test          # 等价于 python3 -m unittest -v service_contract governance_contract
```

`service_contract` 保持健康检查契约；`governance_contract` 覆盖三岗门禁、定向暂停、版本替换留痕、发布/同步幂等、失联恢复、纠正通知送达、反馈去重、持久化与 HTTP 契约（共 32 例）。
