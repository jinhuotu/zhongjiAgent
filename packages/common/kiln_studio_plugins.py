"""受控算法扩展清单；只加载代码仓库明确配置的模块，不扫描上传目录。"""

from importlib import import_module

# 新算法实现位于代码仓库；模块须导出 register() 并调用 engine.register_algorithm。
ALGORITHM_MODULES: tuple[str, ...] = ()


def install_algorithms():
    for name in ALGORITHM_MODULES:
        if not name.startswith("common."):
            raise ValueError("算法扩展模块须位于受控 common 包中")
        register = getattr(import_module(name), "register", None)
        if not callable(register):
            raise ValueError(f"算法扩展 {name} 缺少 register()")
        register()
