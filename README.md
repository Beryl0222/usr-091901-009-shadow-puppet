# 皮影传习与藏品养护

同时管理老皮影养护、道具借用、剧目版本和学徒成长记录。项目以领域契约约定参与者、状态和不可破坏的业务原则；领域层（`domain.py`）以只追加事件保存全部事实，基础服务（`service.py`）提供 JSON 接口。

## 业务原则

- 每件皮影、乐器、剧本、道具拥有稳定身份；复制品另立身份并关联原件。
- 环境巡检、病害照片、修复方案、领用归还连续留痕；修复保存材料与前后状态。
- **珍贵原件**进入课堂或演出，须两名不同责任人分别批准。
- 学徒按年龄、师承与已通过技法阶段获得操作范围；缺课、转师、复核不通过均不抹除历程。
- 传统剧目与新编唱词以版本链式关联；公开演出核对全部创作者有效许可。
- 扫码设备断网重试携带幂等键，同一条养护或借出不会记两次。
- 从一件道具可看清保存状态、修复材料、使用责任和参演剧目；从一名学徒可追到成果、指导者与实际练习对象。

## 运行

```bash
python3 service.py --check            # 核对领域契约
python3 service.py --port 8000        # 默认数据文件 data.json
python3 -m unittest -v                # 运行测试
```

## 接口

所有写接口接受 JSON 请求体，并支持 `Idempotency-Key` 请求头或请求体内的 `"幂等键"`。重放同一键返回首次事件且 `"重复": true`，不再记账。

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| GET | `/health` `/contract` | 健康检查、领域契约 |
| GET | `/objects` `/students` `/plays` `/people` `/catalog` | 清单 |
| GET | `/objects/{id}` | 道具全视图（状态、巡检病害、修复材料、借用责任、参演剧目、时间线） |
| GET | `/objects/{id}/timeline` | 连续事件时间线 |
| GET | `/students/{id}` | 学徒全视图（师承历程、考勤、复核、练习对象与指导者） |
| GET | `/plays/{id}` | 剧目版本、创作者许可、演出记录 |
| POST | `/catalog` `/people` | 技法目录、人员建档 |
| POST | `/objects` `/objects/{id}/replicas` | 物件登记、复制品登记 |
| POST | `/objects/{id}/inspections` `/disease-photos` | 环境巡检、病害照片 |
| POST | `/objects/{id}/repairs/start` `/repairs/finish` | 修复开始与完成 |
| POST | `/objects/{id}/approvals` `/borrow` `/return` | 双人批准、领用、归还 |
| POST | `/students` | 学徒建档 |
| POST | `/students/{id}/mentor` `/attendance` `/reviews` `/practice` | 转师、考勤、作品复核、练习 |
| POST | `/plays` `/plays/{id}/versions` `/permissions` `/performances` | 剧目、版本、许可、公开演出 |

事件持久化于 `data.json`（原子写入，崩溃不损坏），未配置路径时使用内存存储。
