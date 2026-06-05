# 更新日志 (CHANGELOG)

## [v1.1.2] - 2026-06-05

### 优化

- LLM 工具返回结果不再附带标题/标签/说明前缀，直接返回正文内容，与 `/总结` 命令格式一致
- 工具日志中增加缓存命中记录，结果日志截断至 70 字符预览，避免刷屏
- 移除未使用的 `debug_mode` 和 `show_download_fail_tip` 配置项

---

<details>
<summary>📋 点击查看历史更新日志</summary>

## [v1.1.1] - 2026-06-05

### 新增

- 黑白名单对 `/总结` / `/强制总结` 命令和 LLM 工具均生效

### 修复

- 修复 `_materialize_audio` fallback 循环吞掉原始异常的问题，透传实际错误信息

## [v1.1.0] - 2026-06-05

### 新增

- 增加 URL 解析回退机制：当未命中已有解析器时，自动尝试将链接作为视频/音频直链提取；若失败，仍返回”未找到支持处理此链接的解析器”。
- 新增两个 LLM 工具：`summary_extract_media_summary`（提取字幕并交由 AI 总结）和 `summary_extract_media_subtitle`（仅提取原始字幕含时间戳），均仅依赖音频转写，无法感知视频画面。工具通过 `FunctionTool` + `add_llm_tools` 动态注册，支持配置开关控制启用/禁用。

### 修复

- 修复 `_materialize_audio` 在音频下载失败时未回退到视频流的问题，增加 audio/video fallback 容错。

### 优化

- 将直链回退解析器独立到 `core/parser/parsers/direct.py`，并将工具逻辑独立到 `core/tools/` 目录，便于后续维护。
- 将 `format_time` / `segments_to_text` 工具函数统一到 `core/tools/`，消除 `main.py` 中的重复代码。
- 提取 `_call_llm_for_summary` 共享方法，`_summarize_video_impl` 与工具共用模板加载/LLM 调用/去 markdown 逻辑。
- 工具注册从 `@filter.llm_tool` 装饰器改为 `FunctionTool` 子类 + `add_llm_tools` 动态注册，支持通过配置热重载控制开关。
- 优化 `DirectMediaParser` 的 `title` 和 `text` 字段，从 URL 提取文件名。
- 移除 `main.py` 中未使用的 `register` 导入（新版 AstrBot 通过 Star 基类自动注册）。

## [v1.0.1] - 2026-06-05

### 优化

- 移除已经废弃的提示词模板

## [v1.0.0] - 2026-06-04

### 新增

- 发布第一个版本
- 支持通过引用的方式获取 URL
- 支持通过正则提取 URL

</details>