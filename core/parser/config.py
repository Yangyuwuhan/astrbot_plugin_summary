from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
import zoneinfo
from collections.abc import Mapping, MutableMapping
from types import MappingProxyType, UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.context import Context
from astrbot.core.star.star_tools import StarTools
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_path


_PLUGIN_NAME = "astrbot_plugin_summary"


def _sync_summary_template_schema() -> None:
    plugin_dir = Path(get_astrbot_plugin_path()) / _PLUGIN_NAME
    prompts_dir = plugin_dir / "core" / "prompts"
    schema_path = plugin_dir / "_conf_schema.json"

    if not schema_path.exists() or not prompts_dir.exists():
        return

    templates = sorted(
        p.name
        for p in prompts_dir.iterdir()
        if p.is_file() and p.suffix.lower() == ".txt"
    )
    if not templates:
        templates = ["default.txt"]

    try:
        with open(schema_path, "r", encoding="utf-8") as f:
            schema = json.load(f)
    except Exception as e:
        logger.warning(f"读取 _conf_schema.json 失败，跳过模板下拉同步: {e}")
        return

    summary_field = schema.get("summary_template")
    if not isinstance(summary_field, dict):
        return

    changed = False
    if summary_field.get("type") != "string":
        summary_field["type"] = "string"
        changed = True
    if summary_field.get("options") != templates:
        summary_field["options"] = templates
        changed = True
    if summary_field.get("default") not in templates:
        summary_field["default"] = templates[0]
        changed = True

    if not changed:
        return

    try:
        with open(schema_path, "w", encoding="utf-8") as f:
            json.dump(schema, f, ensure_ascii=False, indent=4)
    except Exception as e:
        logger.warning(f"写入 _conf_schema.json 失败，模板下拉同步未生效: {e}")


_sync_summary_template_schema()


DEFAULT_PARSERS_TEMPLATE: list[dict[str, Any]] = [
    {
        "__template_key": "bilibili",
        "enable": True,
        "use_proxy": False,
        "cookies": "",
        "video_codecs": "AVC",
        "video_quality": "_720P",
    },
    {
        "__template_key": "douyin",
        "enable": True,
        "use_proxy": False,
        "cookies": "",
    },
    {
        "__template_key": "kuaishou",
        "enable": True,
        "use_proxy": False,
        "cookies": "",
    },
    {
        "__template_key": "weibo",
        "enable": True,
        "use_proxy": False,
        "cookies": "",
    },
    {
        "__template_key": "xhs",
        "enable": True,
        "use_proxy": False,
        "cookies": "",
    },
    {
        "__template_key": "xiaoheihe",
        "enable": True,
        "use_proxy": False,
        "cookies": "",
        "show_body_text": True,
        "video_send_mode": "first",
    },
]


class ConfigNode:
    """
    配置节点, 把 dict 变成强类型对象。

    规则：
    - schema 来自子类类型注解
    - 声明字段：读写，写回底层 dict
    - 未声明字段和下划线字段：仅挂载属性，不写回
    - 支持 ConfigNode 多层嵌套（lazy + cache）
    """

    _SCHEMA_CACHE: dict[type, dict[str, type]] = {}
    _FIELDS_CACHE: dict[type, set[str]] = {}

    @classmethod
    def _schema(cls) -> dict[str, type]:
        return cls._SCHEMA_CACHE.setdefault(cls, get_type_hints(cls))

    @classmethod
    def _fields(cls) -> set[str]:
        return cls._FIELDS_CACHE.setdefault(
            cls,
            {k for k in cls._schema() if not k.startswith("_")},
        )

    @staticmethod
    def _is_optional(tp: type) -> bool:
        if get_origin(tp) in (Union, UnionType):
            return type(None) in get_args(tp)
        return False

    def __init__(self, data: MutableMapping[str, Any]):
        object.__setattr__(self, "_data", data)
        object.__setattr__(self, "_children", {})
        for key, tp in self._schema().items():
            if key.startswith("_"):
                continue
            if key in data:
                continue
            if hasattr(self.__class__, key):
                continue
            if self._is_optional(tp):
                continue
            logger.warning(f"[config:{self.__class__.__name__}] 缺少字段: {key}")

    def __getattr__(self, key: str) -> Any:
        if key in self._fields():
            value = self._data.get(key)
            tp = self._schema().get(key)

            if isinstance(tp, type) and issubclass(tp, ConfigNode):
                children: dict[str, ConfigNode] = self.__dict__["_children"]
                if key not in children:
                    if not isinstance(value, MutableMapping):
                        raise TypeError(
                            f"[config:{self.__class__.__name__}] "
                            f"字段 {key} 期望 dict，实际是 {type(value).__name__}"
                        )
                    children[key] = tp(value)
                return children[key]

            return value

        if key in self.__dict__:
            return self.__dict__[key]

        raise AttributeError(key)

    def __setattr__(self, key: str, value: Any) -> None:
        if key in self._fields():
            self._data[key] = value
            return
        object.__setattr__(self, key, value)

    def raw_data(self) -> Mapping[str, Any]:
        """
        底层配置 dict 的只读视图
        """
        return MappingProxyType(self._data)

    def save_config(self) -> None:
        """
        保存配置到磁盘（仅允许在根节点调用）
        """
        if not isinstance(self._data, AstrBotConfig):
            raise RuntimeError(
                f"{self.__class__.__name__}.save_config() 只能在根配置节点上调用"
            )
        self._data.save_config()


class ConfigNodeContainer:
    """
    配置节点容器, 把 list 的 dict 变成 dict 的对象集合。

    - nodes: list[dict[str, Any]]
    - item_cls 用于包装 dict 成强类型节点
    - key_name 作为属性名访问, 默认为 "__template_key"
    """

    def __init__(
        self,
        nodes: list[dict[str, Any]],
        item_cls: type[ConfigNode],
        key_name="__template_key",
    ):
        self._item_cls = item_cls
        self._key_name = key_name
        self._nodes: dict[str, ConfigNode] = {}
        for node in nodes:
            key = node.get(key_name)
            if not key:
                logger.warning(f"[node] 缺少 {key_name}，已跳过")
                continue
            if key in self._nodes:
                logger.warning(f"[node] {key} 重复配置，已覆盖")
            self._nodes[key] = item_cls(node)

    def __getattr__(self, name: str) -> ConfigNode:
        if name in self._nodes:
            return self._nodes[name]

        if hasattr(self, "_item_cls") and not name.startswith("_"):
            dummy_node = self._item_cls({self._key_name: name, "enable": True})
            self._nodes[name] = dummy_node
            return dummy_node

        raise AttributeError(name)

    def __iter__(self):
        return iter(self._nodes.values())

    def keys(self):
        return self._nodes.keys()

    def items(self):
        return self._nodes.items()


# ================ 插件自定义配置 ==================


class ParserItem(ConfigNode):
    __template_key: str
    enable: bool
    use_proxy: bool | None
    cookies: str | None
    show_body_text: bool | None
    video_send_mode: str | None
    video_codecs: str | None
    video_quality: str | None

    @property
    def name(self) -> str:
        return self._data.get("__template_key")


class ParserConfig(ConfigNodeContainer):
    acfun: ParserItem
    bilibili: ParserItem
    douyin: ParserItem
    instagram: ParserItem
    kuaishou: ParserItem
    ncm: ParserItem
    nga: ParserItem
    tiktok: ParserItem
    twitter: ParserItem
    weibo: ParserItem
    xiaoheihe: ParserItem
    zhihu: ParserItem
    xhs: ParserItem
    youtube: ParserItem

    def __init__(self, nodes: list[dict[str, Any]]):
        super().__init__(nodes, item_cls=ParserItem)

    def platforms(self) -> list[str]:
        return list(self._nodes.keys())

    def enabled_platforms(self) -> list[str]:
        return [k for k, v in self._nodes.items() if getattr(v, "enable", True)]


class PluginConfig(ConfigNode):
    debug_mode: bool

    llm_provider: str
    summary_template: str | None

    show_token_usage: bool
    enable_cache: bool
    processing_timeout: int

    whitelist: list[str]
    blacklist: list[str]

    source_max_size: int
    source_max_minute: int

    show_download_fail_tip: bool
    download_timeout: int
    download_retry_times: int
    common_timeout: int

    proxy: str | None

    parsers_template: list[dict[str, Any]]

    _plugin_name = "astrbot_plugin_summary"

    def __init__(self, config: AstrBotConfig, context: Context):
        super().__init__(config)
        self.context = context
        self.admins_id = self.context.get_config().get("admins_id", [])

        # ---------- 内置配置 ----------
        self.emoji_cdn = "https://cdn.jsdelivr.net/npm/emoji-datasource-facebook@14.0.0/img/facebook/64/"
        self.emoji_style = "FACEBOOK"  # 可选：APPLE、FACEBOOK、GOOGLE、TWITTER

        # ---------- 派生字段 ----------
        self.proxy = self.proxy or None
        self.max_duration = self.source_max_minute * 60
        self.max_size = self.source_max_size * 1024 * 1024

        tz = context.get_config().get("timezone")
        self.timezone = (
            zoneinfo.ZoneInfo(tz) if tz else zoneinfo.ZoneInfo("Asia/Shanghai")
        )

        # ---------- 路径 ----------
        self.data_dir = StarTools.get_data_dir(self._plugin_name)
        self.plugin_dir = Path(get_astrbot_plugin_path()) / self._plugin_name
        self.cache_dir = self.data_dir / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir = self.data_dir / "temp"
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.cookie_dir = self.data_dir / "cookies"
        self.cookie_dir.mkdir(parents=True, exist_ok=True)
        self.prompts_dir = self.plugin_dir / "core" / "prompts"
        self.prompts_dir.mkdir(parents=True, exist_ok=True)

        # 同步可选模板到 _conf_schema.json，供 WebUI 下拉使用
        self.sync_template_options_to_schema()

        # ---------- 模板设置 ----------
        # 模板目录（core/prompts）默认模板名
        if getattr(self, "summary_template", None) is None:
            self.summary_template = "default.txt"

        available_templates = self.list_available_templates()
        if available_templates and self.summary_template not in available_templates:
            self.summary_template = available_templates[0]
            self.save_config()

        # ---------- Parser ----------
        if not self.parsers_template:
            self.parsers_template[:] = deepcopy(DEFAULT_PARSERS_TEMPLATE)
            self.save_config()

        self.parser = ParserConfig(self.parsers_template)

    def list_available_templates(self) -> list[str]:
        if not self.prompts_dir.exists():
            return []
        templates = [
            p.name
            for p in self.prompts_dir.iterdir()
            if p.is_file() and p.suffix.lower() == ".txt"
        ]
        return sorted(templates)

    def sync_template_options_to_schema(self) -> None:
        schema_path = self.plugin_dir / "_conf_schema.json"
        if not schema_path.exists():
            return

        templates = self.list_available_templates()
        if not templates:
            templates = ["default.txt"]

        try:
            with open(schema_path, "r", encoding="utf-8") as f:
                schema = json.load(f)
        except Exception as e:
            logger.warning(f"读取 _conf_schema.json 失败，跳过模板下拉同步: {e}")
            return

        summary_field = schema.get("summary_template")
        if not isinstance(summary_field, dict):
            return

        old_options = summary_field.get("options")
        old_default = summary_field.get("default")
        summary_field["type"] = "string"
        summary_field["options"] = templates
        if old_default not in templates:
            summary_field["default"] = templates[0]

        if old_options == summary_field.get(
            "options"
        ) and old_default == summary_field.get("default"):
            return

        try:
            with open(schema_path, "w", encoding="utf-8") as f:
                json.dump(schema, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.warning(f"写入 _conf_schema.json 失败，模板下拉同步未生效: {e}")

    def add_blacklist(self, umo: str):
        if umo not in self.blacklist:
            self.blacklist.append(umo)
            self.save_config()

    def remove_blacklist(self, umo: str):
        if umo in self.blacklist:
            self.blacklist.remove(umo)
            self.save_config()
