"""面板注册表 + 自动发现

通过 pkgutil.iter_modules 扫描 client/panels/ 目录下所有非下划线开头的模块，
自动注册 PanelBase 子类。新增面板无需修改此文件。

扩展：同时扫描 manifest 声明的 workspace 组件面板（workspace/<module>/panel.py），
通过 manifest.client_panel 入口声明加载。删除 workspace/<module>/ 后 manifest 消失，
面板自动注销。
"""

import importlib
import inspect
import logging
import pkgutil

from client.core.panel_base import PanelBase

logger = logging.getLogger("parcae.panel_registry")

# 侧边栏分组显示顺序（语义序，非字母序）；未知 category 排最后
_CATEGORY_DISPLAY_ORDER = {"main": 0, "monitor": 1, "advanced": 2}


def hidden_panel_ids() -> set[str]:
    """config.toml [client] hidden_panels 声明的面板（id 或模块名）。

    用途：某个面板还在开发/暂时不用时，从配置里关掉，不必删代码。
    读取失败或未配置时返回空集合（= 全部加载，行为与以前一致）。
    同时匹配模块名（chat）与 PANEL_META.id（chat），前者可省掉 import 开销。
    """
    try:
        from lib.config_reader import load_config

        cfg = load_config() or {}
        raw = (cfg.get("client") or {}).get("hidden_panels") or []
        return {str(x).strip() for x in raw if str(x).strip()}
    except Exception as e:  # 配置坏了不该拖死 GUI 启动
        logger.warning("读取 [client] hidden_panels 失败，按全部加载处理: %s", e)
        return set()


class PanelRegistry:
    """面板注册表 + 自动发现"""
    _classes: list[type[PanelBase]] = []

    @classmethod
    def discover(cls, package_name: str = "client.panels") -> int:
        """从 client/panels/ 自动发现所有 PanelBase 子类

        同时扫描 manifest 声明的 workspace 组件面板。
        跳过 config.toml [client] hidden_panels 里声明的面板。

        Returns:
            新发现的 panel 类数量
        """
        new_count = cls._discover_from_package(package_name)
        new_count += cls._discover_from_manifest()
        return new_count

    @classmethod
    def _discover_from_package(cls, package_name: str = "client.panels") -> int:
        """从 client/panels/ 包扫描面板（原 discover 逻辑）"""
        try:
            package = importlib.import_module(package_name)
        except ImportError:
            return 0

        package_path = getattr(package, "__path__", None)
        if not package_path:
            return 0

        hidden = hidden_panel_ids()
        existing_ids = {c.meta().id for c in cls._classes if c.PANEL_META is not None}
        new_count = 0

        for _, name, _ in pkgutil.iter_modules(package_path):
            if name.startswith("_"):
                continue
            if name in hidden:
                # 模块名命中：连 import 都不做（chat 这类重面板可省数百毫秒）
                logger.info("panel %s 在 [client] hidden_panels 中，跳过加载", name)
                continue
            module_name = f"{package_name}.{name}"
            try:
                module = importlib.import_module(module_name)
            except Exception as e:
                # 模块加载失败不阻断其他面板发现
                logger.warning("加载 %s 失败: %s", module_name, e)
                continue

            for _attr_name, attr in inspect.getmembers(module, inspect.isclass):
                if (issubclass(attr, PanelBase)
                        and attr is not PanelBase
                        and attr.PANEL_META is not None
                        and attr.__module__ == module.__name__):
                    panel_id = attr.meta().id
                    if panel_id in hidden:
                        # 模块名没命中但 id 命中（文件名与 id 不一致时的兜底）
                        logger.info("panel %s 在 [client] hidden_panels 中，不注册", panel_id)
                        continue
                    if panel_id in existing_ids:
                        continue
                    cls._classes.append(attr)
                    existing_ids.add(panel_id)
                    new_count += 1

        return new_count

    @classmethod
    def _discover_from_manifest(cls) -> int:
        """从 manifest 声明的 client_panel 入口发现面板（workspace/<module>/panel.py）

        按 manifest.client_panel.file 加载模块，按 manifest.client_panel.class_name 找
        PanelBase 子类。删除 workspace/<module>/ 后 manifest 消失，面板自动注销。
        """
        try:
            from lib.component_manifest import load_manifests
            manifests = load_manifests()
        except Exception as e:
            logger.warning("manifest 加载失败，跳过组件面板发现: %s", e)
            return 0

        existing_ids = {c.meta().id for c in cls._classes if c.PANEL_META is not None}
        hidden = hidden_panel_ids()
        new_count = 0

        for name, m in manifests.items():
            if not m.enabled or m.client_panel is None:
                continue
            if name in hidden:
                logger.info("组件面板 %s 在 [client] hidden_panels 中，跳过加载", name)
                continue
            # 按 manifest 声明的 file 动态加载模块（去掉 .py 后缀）
            module_name = f"workspace.{name}.{m.client_panel.file[:-3]}"
            try:
                module = importlib.import_module(module_name)
            except Exception as e:
                # 8-6: 与 _discover_from_package 对齐——组件 panel.py 的模块级
                # SyntaxError/AttributeError 等任意异常不能从 discover() 炸穿到
                # MainWindow.__init__（一个写坏的组件面板拖死整个 GUI 启动）
                logger.warning("manifest 组件 %s 面板模块加载失败 (%s): %s", name, module_name, e)
                continue

            # 按 manifest.client_panel.class_name 找 PanelBase 子类
            panel_class = getattr(module, m.client_panel.class_name, None)
            if (panel_class is None
                    or not inspect.isclass(panel_class)
                    or not issubclass(panel_class, PanelBase)
                    or panel_class is PanelBase
                    or panel_class.PANEL_META is None):
                logger.warning("manifest 组件 %s 面板类 %s 无效", name, m.client_panel.class_name)
                continue

            if panel_class.meta().id in hidden:
                logger.info("组件面板 %s 在 [client] hidden_panels 中，不注册", panel_class.meta().id)
                continue
            if panel_class.meta().id in existing_ids:
                continue
            cls._classes.append(panel_class)
            existing_ids.add(panel_class.meta().id)
            new_count += 1

        return new_count

    @classmethod
    def get_all_sorted(cls) -> list[type[PanelBase]]:
        """按 (category 显示序, order) 排序返回所有已注册面板类

        category 用语义顺序（主面板 → 监控 → 高级），不用字母序——
        字母序会把高频的 main 组压到 advanced 组下面。
        """
        return sorted(
            cls._classes,
            key=lambda c: (_CATEGORY_DISPLAY_ORDER.get(c.meta().category, 99), c.meta().order),
        )

    @classmethod
    def get_by_id(cls, panel_id: str) -> type[PanelBase] | None:
        for c in cls._classes:
            if c.meta().id == panel_id:
                return c
        return None

    @classmethod
    def clear(cls) -> None:
        """清空注册表（仅供测试用）"""
        cls._classes.clear()
