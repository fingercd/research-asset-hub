"""按资源域拆分的 v1 路由模块。

子模块在导入时向 apps.api.routes_v1 的共享 router 注册路由；
本包由 routes_v1 在模块底部导入，导入本包即注册全部路由。
"""

from . import (  # noqa: F401
    annotations,
    assets,
    backups,
    code_graph,
    comparisons,
    domains,
    graph,
    integrations,
    ops,
    overrides,
    relations,
    settings,
)
