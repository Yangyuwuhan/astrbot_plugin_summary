# astrbot_plugin_summary

一个面向 AstrBot 的视频/音频总结插件。

插件会根据用户输入的 URL 进行解析与下载资源，提取音频后调用必剪转写接口生成字幕，再交给 LLM 输出结构化中文总结。

## 功能特性

- 支持多平台 URL 解析
- 借助必剪接口实现音频转文字
- 本地硬盘缓存，重复 URL 直接从本地获取字幕和总结内容，降低成本
- 支持强制总结，复用本地字幕并重新调用 LLM，覆盖旧总结，防止偶然错误

## 工作流程

1. 用户发送 `/总结 url` 或 `/强制总结 url`指令
2. 解析器识别并解析目标平台链接
3. 下载媒体并提取音频
4. 调用必剪接口进行字幕转写
5. 按提示词模板构建 Prompt 并调用 LLM
6. 返回总结结果，并按配置写入本地缓存

## 安装方式

下面本仓库zip文件，在astrbot仪表盘安装

## 配置说明

- `llm_provider`：总结使用的模型提供商，留空使用全局 LLM
- `show_token_usage`：是否在结尾输出 token 使用情况与总结耗时
- `enable_cache`：是否启用本地缓存
- `processing_timeout`：LLM 总结超时秒数
- `summary_template`：总结模板选择（WebUI 下拉显示 `core/prompts/` 下的 txt 文件）
- `whitelist` / `blacklist`：白名单/黑名单
- `source_max_size` / `source_max_minute`：下载资源大小与时长限制
- `download_timeout` / `download_retry_times` / `common_timeout`：下载与请求超时控制
- `proxy`：全局代理地址
- `parsers_template`：各平台解析器开关与参数

## 缓存策略

当 `enable_cache=true`：

- `/总结 url`：若命中 URL 对应总结缓存，直接返回缓存总结
- `/强制总结 url`：若命中本地字幕缓存，跳过下载与转写，直接交给 LLM 重新总结并覆盖旧缓存总结

缓存数据储存在 `data/plugin_data/astrbot_plugin_summary/cache/`

## 注意：

- 环境中需存在 `ffmpeg`
- 可在`data/plugins/astrbot_plugin_summary/core/prompts/`中添加`.txt`文件来自定义输出模板。注意重载插件以加载新模板。
- 本插件的工作依赖于音频转文字，并非直接浏览视频
- 本插件采用 **vibe coding**，作者已对其功能进行严格审查，但不保证插件稳定性

## 致谢

- 本项目使用了 [astrbot_plugin_parser](https://github.com/Zhalslar/astrbot_plugin_parser) 的部分代码实现资源下载
- 本项目受 [astrbot_plugin_biliVideo](https://github.com/storyAura/astrbot_plugin_biliVideo) 启发，使用必剪的接口实现视频字幕提取
- 本项目使用了 [astrbot_plugin_markdown_killer](https://github.com/AlanBacker/astrbot_plugin_markdown_killer) 的主要逻辑实现对 markdown格式 的严格剔除

## 许可证

MIT LIENCE