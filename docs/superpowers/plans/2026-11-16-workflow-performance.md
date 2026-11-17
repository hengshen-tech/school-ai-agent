# 单 Agent 批改实施记录

最终用户要求：取消 MCP 和多代理；理解材料、独立解题、学生答案对照都写在一个大 Agent 的 Prompt 中，由它自主完成，不由后端编排多个模型阶段。保留用途识别和学生卷 OCR 作为输入准备。

已实现：
- submission_agent.py 组装一次消息，包含全部上传图片、必要文字、学生页 OCR 和存档参考图；去重相同参考图片。
- SINGLE_SUBMISSION Prompt 定义完整任务与最终结果格式。SDK 单次响应，关闭所有工具、MCP 和子代理；不自动重跑整份任务。
- 后端仅校验、保存模型最终 JSON 与绘制标记。单次独立解题结果标记 ai_solved，不伪装为第二次独立审核。
- 移除旧 MCP 工具、工作流 Hook、原生子代理入口与角色状态视图。保留历史数据校验兼容。
- 新任务界面合并显示理解、解题和批改，不分别统计无法准确测量的模型内部阶段时间。

官方参考：https://code.claude.com/docs/en/agent-sdk/python 和 https://code.claude.com/docs/en/agent-sdk/streaming-vs-single-mode 。

验证范围：单 Agent 输入与 SDK 配置、严格完成状态、老师答案冲突、建库后答题卡复用、OCR 筛选/缓存、前端合并步骤。仅相关检查，不运行真实付费模型请求。
