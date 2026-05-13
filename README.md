# astrbot_plugin_summary

一个面向 AstrBot 的视频/音频总结插件。

插件会根据用户输入的 URL 进行解析与下载，提取音频后调用必剪转写接口生成字幕，再交给 LLM 输出结构化中文总结。

## 功能特性

- 支持命令：`/总结 <url>`
- 支持命令：`/强制总结 <url>`
- 多平台解析器（通过 `parsers_template` 配置）
- 本地 JSON 缓存（URL / 字幕 / 总结）
- 命中缓存时可直接返回总结，降低重复调用成本
- 强制总结模式可复用本地字幕并重新调用 LLM，覆盖旧总结

## 工作流程

1. 用户发送 `/总结 url` 或 `/强制总结 url`
2. 解析器识别并解析目标平台链接
3. 下载媒体并提取音频（`ffmpeg`）
4. 调用必剪接口进行字幕转写
5. 按提示词模板构建 Prompt 并调用 LLM
6. 返回总结结果，并按配置写入本地缓存

## 目录结构

```text
astrbot_plugin_summary/
  main.py
  _conf_schema.json
  metadata.yaml
  requirements.txt
  core/
    parser/
    prompts/
      default.txt
    transcriber/
```

## 安装方式

将插件仓库克隆到 AstrBot 的插件目录：

```bash
git clone <your-repo-url> AstrBot/data/plugins/astrbot_plugin_summary
```

安装依赖：

```bash
pip install -r AstrBot/data/plugins/astrbot_plugin_summary/requirements.txt
```

确保系统可用 `ffmpeg` 命令。

## 配置说明

插件配置由 `_conf_schema.json` 定义，主要配置项包括：

- `llm_provider`：总结使用的模型提供商
- `show_token_usage`：是否在结尾输出 token 与耗时
- `enable_cache`：是否启用本地缓存
- `processing_timeout`：LLM 总结超时秒数
- `summary_template`：总结模板选择（WebUI 下拉显示 `core/prompts/` 下的 txt 文件）
- `whitelist` / `blacklist`：会话级解析白名单/黑名单
- `source_max_size` / `source_max_minute`：下载资源大小与时长限制
- `download_timeout` / `download_retry_times` / `common_timeout`：下载与请求超时控制
- `proxy`：全局代理地址
- `parsers_template`：各平台解析器开关与参数

## 缓存策略

当 `enable_cache=true`：

- `/总结 url`：若命中 URL 对应总结缓存，直接返回缓存总结
- `/强制总结 url`：若命中本地字幕缓存，跳过下载与转写，直接交给 LLM 重新总结并覆盖旧缓存总结

缓存目录位于 AstrBot 数据目录下的插件数据目录中。

## 开发与调试

- 修改插件代码后，可在 AstrBot WebUI 中重载插件
- 建议在提交前进行基础语法检查

```bash
python -m py_compile AstrBot/data/plugins/astrbot_plugin_summary/main.py
```

## 发布建议（参考 AstrBot 文档）

- 保持 `metadata.yaml` 信息完整
- 不要把运行时数据放在插件目录；应写入 AstrBot 的 `data` 数据目录
- 清理 `__pycache__`、临时文件和本地测试数据后再发布
- 插件仓库建议附带 `.gitignore`，避免上传无关文件
