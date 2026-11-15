# 多材料作业输入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** DeepSeek OCR 默认选中；同一学生的多文件、多页材料先加入列表，点击开始后统一识别用途并批改。

**Architecture:** 保持一份批改对应一个后台任务。上传接口同时兼容旧 `file` 和新 `files`，保存有序输入清单与用途选择。复用逐页解析调用识别材料角色，合并试卷题干、标准答案和学生答案，不新增独立位置复核 Agent。学生页参与判分；所有材料保留查看，导出仅包含学生页。

**Tech Stack:** FastAPI / 文件存储 / Pillow / 原生 JavaScript / pytest / node:test。

---

### Task 1: 输入与重试

Files: `backend/app/main.py`, new `backend/app/materials.py`, `tests/test_material_bundle.py`.

- [x] 写上传回归：`files=[a.png,b.pdf]` 保存一个任务，省略识别方式默认 OCR；用途、顺序影响缓存；错误类型、空文件和大小超限拒绝。
- [x] 运行 `/Users/yazhou/miniconda3/bin/python -m pytest tests/test_material_bundle.py -q`，确认新行为失败。
- [x] 实现 `inputs=[{original,filename,role}]`，保留旧 `original`；用版本化的文件内容摘要和角色生成组合指纹。限制 20 文件、单个 40MB、合计 200MB。
- [x] 重试及重新批改复制全部原件和角色，保留输入顺序；旧单文件仍兼容。

### Task 2: 材料识别与对齐

Files: `backend/app/materials.py`, `backend/app/pipeline.py`, `backend/app/prompts.py`, `tests/test_material_bundle.py`.

- [x] 写混合材料测试：试卷 + 标准答案 + 两张学生作答页，只有学生页进入评分，标准答案进入老师答案路径，题干由解析 Agent 整体阅读试卷和作答后补全，代码不按题号拼接。
- [x] 整组解析返回 `materials=[{page,role,confidence}]`、带学生页码的 `questions` 和 `reference_answers`，角色枚举为 `student_work/question_paper/answer_key/unknown`。用户明确选择的用途优先。
- [x] 不将学生作答当作标准答案；没有标准答案时沿用 AI 解题。含糊角色、缺题干、冲突标准答案、多位学生给出具体错误，不猜测。
- [x] 转换所有输入为连续页，保存来源页与用途到 `materials.json` 和结果。只给学生页问题建立坐标；PDF 导出选择学生页。
- [x] 单图、旧记录、同文件混合页面、缺标准答案和多页 PDF 纳入验证。

### Task 3: 待开始文件列表

Files: `frontend/index.html`, `frontend/app.js`, `frontend/style.css`, `tests/test_frontend.cjs`.

- [x] 测试拖拽、文件选择均不发送请求；支持追加、移除、用途选择；点击开始仅发送一次包含全部文件的请求，失败保留材料，提交期间阻止重复点击。
- [x] 首页及任务详情使用同一种待开始列表，默认“自动识别”，可改为试卷/标准答案/学生作答。DeepSeek OCR 可用时默认选择；不可用时视觉识别仍可用。
- [x] 保留导航竞态保护；任务详情轮询不覆盖未开始的列表。开始成功才清除本次文件及标准答案输入。
- [x] 结果页面显示材料来源与用途；默认打开第一张学生作答页。

### Task 4: 验证与复核

Files: `README.md`, relevant regression tests.

- [x] 更新多材料、默认 OCR 和点击开始说明。
- [x] `/Users/yazhou/miniconda3/bin/python -m pytest tests -q`；`node --test tests/test_frontend.cjs`；`/Users/yazhou/miniconda3/bin/python -m ruff check backend/app tests --select F`。
- [x] 独立 Subagent 审查并修复明确问题，再完成针对性验证。仓库无 Git 元数据，不执行提交；Docker 权限限制单独如实说明。

实现记录：前后端需求审查均通过；质量审查已修复跨任务草稿混入、已有草稿OCR选择失效、PDF渲染前预检、重批及其重试保护老师答案。最终验证：97 项 Python、40 项 Node 测试通过；Ruff F 与 JS 语法检查通过。需求及质量复审均通过，无未关闭的重要问题。未调用真实模型，未更新受权限限制的 Docker 容器。
