# 调研笔记 06 · 火山 LAS 具身在线算子拆解

核实日期为 2026-11-24。下表依据火山引擎公开 API 和发布记录整理，用来设计 Ego 视频的异步加工服务。账户权限、线上返回值、时延和费用均待实际调用验证。表中“本地用途”是项目设计。可从[在线算子清单](https://docs.volcengine.com/docs/lake-ai-service/list-of-supported-operators-online-operators?lang=zh)查到对应产品入口。

## 1. 哪些能力值得对标

以下算子在核对的接口页均使用 `operator_version=v1`。模板另有自己的名称或版本。2026 年 10 月的发布记录将手部姿态、具身数据预检、具身标注质检列为需申请开白的算子。其余两项的可调用性也需在目标地域和账号下验证。[LAS 发布记录](https://docs.volcengine.com/docs/lake-ai-service/feature-release-record-2026?lang=zh)

| LAS 算子 | 输入与使用条件 | 调用方拿到什么 | 本地用途 |
| --- | --- | --- | --- |
| 具身数据预检 `las_embodied_collection_precheck` | 单路采集视频，可选择检查项 | 视频技术质量判定和具体问题，如冻结或抖动 | 放在昂贵的 GPU 加工之前。保留问题区间供复核。[预检 API](https://docs.volcengine.com/docs/lake-ai-service/embodied-data-precheck?lang=zh) |
| 手部姿态估计 `las_handpose_estimation` | 单目图像、单目视频，或真实标定且时间同步的双目视频 | 左右手、21 点关键点及按请求生成的相机位姿、局部世界轨迹 | 对标 `op-per-hand` 的交付物，核对坐标约定、观测来源和降级。[手部 API](https://docs.volcengine.com/docs/lake-ai-service/hand-pose-estimation?lang=zh) |
| 具身空间标注 `las_spatial_perception` | 图像或视频，选用相应任务模板 | 机械臂或所交互物品的框、掩码、跟踪结果，交互物品还带时间区间 | 将工具轨迹和手轨迹按源时间对齐，供手物交互复核。[空间标注 API](https://docs.volcengine.com/docs/lake-ai-service/spatial-perception?lang=zh) |
| 具身语义标注 `las_embodied_caption` | 操作视频，可限定时段或给候选任务 | 交互物品、动作起止时间、描述与技能标签 | 接入语义标注，对照视频检查动作边界。[语义标注 API](https://docs.volcengine.com/docs/lake-ai-service/embodied-semantic-annotation?lang=zh) |
| 具身标注质检 `las_embodied_annotation_quality_check` | 视频和待核标注，或已完成的单例质检结果 | 视频、片段、动作时间线的审核结果或批次报告 | 进入标注复核队列，几何精度另用手部与相机真值评估。[标注质检 API](https://docs.volcengine.com/docs/lake-ai-service/embodied-annotation-quality-inspection?lang=zh) |

## 2. 跟一次手部任务

以头戴相机拍摄的理线视频为例，下列 JSON 是按公开接口构造的请求和结果摘录。示例媒体、任务 ID 和 URL 均为示意。假设调用者选取单目针孔画面，允许服务估计内参，需要源视频第 5 到第 12 秒的世界轨迹。[手部 API 的 Submit 和 Poll](https://docs.volcengine.com/docs/lake-ai-service/hand-pose-estimation?lang=zh)

1. **提交**。向所在地域的 `/api/v1/submit` 提交下方请求。媒体须可被服务读取，视频应有有效 PTS。双目任务改用 `stereo_hand_pose_estimation@v1`，附真实 Rig 标定、左右目组织和同步时间轴。单目世界系模式需要与去畸变画面一致的针孔内参，或请求估计针孔内参。

```json
{
  "operator_id": "las_handpose_estimation",
  "operator_version": "v1",
  "data": {
    "task_template": "mono_hand_pose_estimation@v1",
    "media": {"type": "video", "uri": "tos://example/ego-left.mp4", "fps": 10},
    "video_clip": {"start": 5, "end": 12},
    "task_context": {"camera": {"enable_estimation": true}, "world_coordinates": {"enabled": true}}
  }
}
```

2. **查询**。保存 Submit 返回的 `task_id`。使用相同账号、算子 ID 和版本向 `/api/v1/poll` 查询。等待终态并检查业务码，记录错误或取回结果清单。收到并发限制时按服务返回的退避提示处理。

```json
{
  "metadata": {"task_id": "example-hand-01", "task_status": "COMPLETED", "business_code": "0"},
  "data": {"result_manifest_url": "https://example.org/results/hand-01.json"}
}
```

3. **解释清单**。先读 `world_coordinates.status`（`applied`、`degraded` 或 `unavailable`），再读帧上 `camera_pose.valid` 及每只手的 `keypoints_3d_world.valid`。两只手逐一判定。保留 `observation` 描述的检测、插值或补全来源。裁剪返回的时间戳仍在源视频时间轴上。单目尺度可能来自估计，双目消费还要看配对和逐点质量。[手部 API 的结果结构与错误码](https://docs.volcengine.com/docs/lake-ai-service/hand-pose-estimation?lang=zh)

## 3. 迁移到我们的产线

| 从案例得到的问题 | 本地设计选择 |
| --- | --- |
| 对面的人也伸出了右手，如何确定操作者？ | 把手别、任务内轨迹 ID 和操作者归属分开存储。留存判断依据；归属未知的轨迹可供复核。 |
| 临时结果地址失效后，训练如何追到这次输出？ | 入湖前保存受管的不可变结果、校验值和来源清单。数据集引用受保护的版本，供应商任务 ID 用于审计。 |
| 要求世界轨迹的任务只拿到部分有效帧，按什么计量？ | 用消费策略裁出合格区间。供应商按输入或处理时长报告的用量单独留账，按项目有效小时口径计算单位成本。 |
| 更换相机模型后，哪些手部输出需要更新？ | 记录手部使用内置位姿还是外部相机产物，以及实际字段依赖。LAS 的接口页公开了输出语义，其内部模型实现仍属未知。 |

本地请求、结果样例与验收见[算子设计 09](../02-生产域/算子设计/算子设计-09-具身在线算子调用与结果契约.md)。单位成本见[算子成本模型](../02-生产域/算子设计/算子设计-06-算子成本模型.md)。
