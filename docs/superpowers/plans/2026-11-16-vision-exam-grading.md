# 纯视觉单 Agent 批改

目标：移除新任务的 DeepSeek OCR 与单独用途分类；将用户提供的 exam-grading 内容用作主 Prompt，单 Agent 自主读图、动态裁剪放大、解题、复核和定位。后端仅转换输入、限制工具作用域、校验、保存、绘图和导出。

实现：
1. API 接受任意已配置的图像模型，旧 ocr_mode 参数兼容但统一按 vision；去掉 OCR 设置和可调用接口。
2. SDK 单会话启用内置 Read/Bash，不注册 MCP 或子代理；默认 200 轮，受环境变量 EXAM_AGENT_MAX_TURNS 控制。Bash 仅运行任务限定的裁图/算术脚本，Read 仅允许本任务原图和裁图片。
3. 保存 exam-grading 原文，追加系统 JSON 字段、题号层级、老师答案优先级及原图坐标映射约定。模型返回 materials，与 questions、answers、results 一起由后端校验。
4. 新结果使用视觉坐标，红勾/红叉/黄问号；清晰定位的待复核题可显示黄问号，改判后重绘；无法定位保留人工框选。历史 OCR 结果只保留读取兼容，不再发起 OCR 请求。
5. 相关检查覆盖 OCR 永不调用、非 DeepSeek 地址上传、工具裁图映射/越界拦截、视觉标记颜色和多页 PDF、题号及人工复核。
