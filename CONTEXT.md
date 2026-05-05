# KBImporter

从 Get 笔记拉取笔记并写入 Notion 的 CLI 工具，支持三种笔记类型，分两条命令运行。

## Language

### 笔记类型

**菜谱笔记 (Recipe Note)**:
来自菜谱知识库（指定 `topic_id`）的笔记，含抖音链接，经 upsert 写入 Notion 菜谱数据库。
_Avoid_: 菜谱、recipe

**AI链接笔记 (AI Link Note)**:
带有"AI链接笔记"标签的笔记，自动写入 Notion card-notes 数据库，无需人工介入。
_Avoid_: AI 链接、ai link

**录音卡笔记 (Audio Card Note)**:
带有"录音卡笔记"标签的笔记，需经本地 ASR 纠错和人工审阅后写入 Notion，由 `audio` 命令处理。
_Avoid_: 录音卡、audio card

### 流程

**同步流程 (Sync Flow)**:
`sync` 命令执行的全自动流程：依次运行菜谱同步和 AI 链接同步，无人工介入。
_Avoid_: import flow（此术语在代码中已被用于指代 AI链接 + 录音卡，含义不同）

**录音卡流程 (Audio Card Flow)**:
`audio` 命令执行的交互式流程：每条笔记依次经 ASR 纠错、编辑器审阅、目标数据库选择后写入 Notion。

**菜谱同步 (Recipe Sync)**:
同步流程的第一阶段，从指定知识库拉取菜谱笔记，解析抖音链接，upsert 到 Notion 菜谱数据库，状态由菜谱状态文件维护。

**AI链接同步 (AI Link Sync)**:
同步流程的第二阶段，从所有知识库拉取笔记，经路由规则过滤出 AI链接笔记，自动写入 Notion，状态由 AI 链接水位维护。

### 状态管理

**水位 (Watermark)**:
导入流程（AI链接 + 录音卡）使用的时间戳-based 状态文件，记录最近一次已处理笔记的 `created_at` 及同时刻已处理的 `note_id` 列表，用于跳过已见笔记。AI链接和录音卡各有独立水位文件。
_Avoid_: 状态文件（与菜谱状态文件含义重叠）

**菜谱状态 (Recipe State)**:
菜谱同步使用的 ID-based 状态文件，按 `topic_id` 记录最后同步的 `last_note_id`，用于服务端增量拉取（`since_id` 参数）。
_Avoid_: 水位（水位特指时间戳 based，菜谱状态是 ID-based）

### 路由与过滤

**路由规则 (Routing Rules)**:
`config/routes.json` 中按标签匹配的规则集，仅作用于导入流程（AI链接 + 录音卡），决定笔记应写入哪条子流程或被跳过。菜谱笔记在导入流程外单独处理，不经过路由规则。

**全局排除 (Global Exclude)**:
路由规则中的 `global_exclude` 标签列表，匹配后直接跳过，不进入具体规则匹配。当前仅用于排除带"菜谱"标签的笔记。

### 纠错

**纠错词表 (Correction Dictionary)**:
`config/correction_dict.json`，存储 ASR 错误词到正确词的映射，分为自动替换（`auto_replace`）和待确认（`manual_confirm`）两类，在录音卡流程中自动从用户编辑中学习新映射。

**智能总结 (Zhizhi Summary)**:
Get 笔记 API 返回的 `content` 字段，录音卡笔记的 ASR 转录结果，是纠错和审阅的输入来源。
_Avoid_: 原始内容、content

## Relationships

- 一条**菜谱笔记**属于一个菜谱知识库 topic，由**菜谱同步**处理
- 一条**AI链接笔记**或**录音卡笔记**由**路由规则**决定归属流程
- **同步流程**维护两个独立状态：**菜谱状态**（ID-based）和 **AI链接水位**（时间戳-based）
- **录音卡流程**维护独立的**录音卡水位**，与 AI链接水位互不干扰
- **纠错词表**仅在**录音卡流程**中使用，AI链接同步不做纠错

## Example dialogue

> **Dev:** "如果一条笔记同时带有'AI链接笔记'和'录音卡笔记'标签，会发生什么？"
> **Domain expert:** "`NoteRouter` 按规则顺序匹配，先命中'AI链接笔记'规则，走 AI链接同步。同一条笔记不会进入录音卡流程。"

> **Dev:** "菜谱笔记会不会被路由规则处理到？"
> **Domain expert:** "不会。菜谱笔记走独立的菜谱同步，由 topic 过滤在 API 层面隔离。导入流程的全局排除是第二道防线，依赖手动打'菜谱'标签，不是主要保障。"

## Flagged ambiguities

- **"import"** 在代码中（`import_writer.py`、`iter_import_notes`）指 AI链接 + 录音卡的导入流程，不含菜谱同步。口语中"导入"可能泛指所有三类，注意区分。
- **"水位"** 在代码层面有两种机制：时间戳-based（导入流程用）和 ID-based（菜谱同步用）。术语上用**水位**专指前者，**菜谱状态**指后者。
