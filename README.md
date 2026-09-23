# 皮影传习与藏品养护

同时管理老皮影养护、道具借用、剧目版本和学徒成长记录的后台。以领域契约
（`domain_contract.json`）约定参与者、状态和不可破坏的业务原则，领域层
`domain.py` 承载全部业务规则，`service.py` 提供 HTTP 接口。

## 运行

```bash
python3 service.py --check            # 核对服务与契约配置
python3 service.py --port 8000        # 启动（仅内存，重启清空）
python3 service.py --data state.json  # 启动并持久化到 JSON 快照
python3 -m unittest -v                # 运行全部测试
```

启动后可访问 `/health` 与 `/contract`。

## 核心规则

- **稳定身份**：每件皮影、乐器、剧本编号唯一且永不复用；复制品单独编号并与原件关联。
- **连续记录**：环境巡检、病害照片、养护、修复（方案、材料、修复前后状态）、领用归还
  全部只追加，并汇成按时间排列的藏品时间线。
- **两人批准**：珍贵原件进入课堂或演出必须经两名**不同**责任人批准，批准随借用单永久保存。
- **学徒范围**：按年龄（如操控满 8 岁、雕刻满 12 岁、道具修缮满 16 岁）、师承是否覆盖该
  技法、已通过阶段（初学→进阶→熟练→出师，须逐级）共同判定；复制品入门可用，普通原件
  须进阶，珍贵原件须出师且仍需两人批准。原件进课堂还须持有有效教学借用单。
- **历程不抹去**：缺课、转师、阶段通过、作品复核（含复核不通过）均只追加。
- **版本与许可**：传统剧目与新编唱词以版本关联；待授权或已过期版本不得公开演出，演出
  登记时逐件核对道具演出借用批准。
- **断网幂等**：扫码事件携带稳定 `event_id`，经 `/sync` 批量补传；同一事件重放（含跨
  重启重放）只返回首次结果并标注 `duplicate`，不会重复记养护或借出。

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/persons` | 登记责任人/教师（含可授技法） |
| POST | `/artifacts` | 登记藏品（`tier`: 复制品/普通原件/珍贵原件；复制品传 `replica_of`） |
| POST | `/inspections` | 环境巡检（库位、温湿度、通风除尘） |
| POST | `/artifacts/{code}/diseases` | 病害与照片，自动转限制使用 |
| POST | `/artifacts/{code}/conservations` | 日常养护 |
| POST | `/artifacts/{code}/repairs/start`、`.../finish` | 修复起止（材料与前后状态） |
| POST | `/loans`、`/loans/{id}/return` | 领用批准与归还核验 |
| POST | `/apprentices` | 登记学徒（出生日期、师承） |
| POST | `/apprentices/{id}/certifications`、`/absences`、`/mentor`、`/reviews` | 阶段认定、缺课、转师、作品复核 |
| POST | `/lessons` | 授课练习（指导者、学徒、实际使用原件或复制品，全留痕） |
| POST | `/plays`、`/plays/{id}/versions` | 剧目与版本许可 |
| POST | `/performances` | 公开演出登记（核对许可与借用） |
| POST | `/sync` | 断网恢复后事件批量补传（幂等） |
| GET | `/artifacts/{code}` | 一件道具：保存状态、病害、修复材料、使用责任、参演剧目与完整时间线 |
| GET | `/apprentices/{id}` | 一名学徒：师承史、阶段、历程、复核，以及指导者与实际练习对象 |
| GET | `/apprentices/{id}/can-operate?artifact_code=&skill=&date=` | 操作范围判定 |

业务错误返回 `422`（规则违反）、`409`（状态冲突/重复登记）、`404`（对象不存在），
响应体形如 `{"error": "…", "code": "…"}`。

### 离线同步示例

```jsonc
POST /sync
{"events": [
  {"type": "conserve", "artifact_code": "P002", "action": "通风除尘",
   "operator": "g1", "event_id": "SCANNER-A-0007"}
]}
```

每条结果带 `ok` 与 `duplicate`；单条失败不影响同批其他事件。
