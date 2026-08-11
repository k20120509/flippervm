"""版本号单一事实源。所有展示与打包版本都从这里读。"""

__version__ = "1.0.0"
__version_tuple__ = tuple(int(x) for x in __version__.split("."))

APP_NAME = "FlipperVM"
APP_TITLE = f"{APP_NAME} v{__version__}"
