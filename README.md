# 公共健身资产问效

服务用于连接公益资金、健身设施、使用情况和维护责任，为后续建设决策提供可核验依据。

项目当前提供稳定的基础服务入口，便于本地联调和运维巡检。运行 `python3 service.py --check` 可核对服务配置；执行 `python3 service.py --port 8000` 后访问 `/health` 可确认服务身份。

## 扫码健身指导内容治理

`governance.py` 提供指导内容治理后端，HTTP 接口挂在同一服务上：

- `POST /facilities`、`POST /qrcodes`：登记设施（型号、公园、位置）并把二维码绑定到设施。
- `POST /facilities/{id}/events`：上报设施迁移、部件更换、风险事件，只暂停受影响二维码上的内容并生成替代提示。
- `POST /guidance`、`POST /guidance/{id}/versions`：登记指导内容版本（适用人群、禁忌条件、动作步骤、兼容器材型号）。
- `POST /guidance/{id}/versions/{v}/reviews`：运动医学、无障碍、运营三类审核；全部通过后才能 `POST .../publish` 发布，重复发布幂等。
- `POST /terminals/{id}/sync`：边缘终端上报缓存并取回当前有效内容，重复同步不产生重复发布；失联恢复后必须 `POST /terminals/{id}/confirm` 确认当前有效版本。
- `POST /terminals/{id}/notices/ack`：终端确认收到纠正通知。
- `POST /feedback`、`GET /feedback/summary?qr_code=`：匿名反馈"看不懂/完成不了"，同一报告者重复提交只计一次。
- `GET /guidance/{id}/audit`、`GET /notices/audit`、`GET /qrcodes/{qr}/current`：核对一条指导何时对哪些设施生效、为什么被替换、纠正通知是否送达曾缓存旧内容的终端，以及市民扫码当前应看到的内容。

运行 `python3 -m unittest service_contract test_governance`（或 `npm test`）执行全部测试。
