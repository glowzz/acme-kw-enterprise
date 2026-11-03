"""授权校验器：自用扩展，固定返回有效。

KnowWork 的授权机制保护的是扩展作者的商业代码（开源侧不做签名校验）；我们自己就是
作者，没有需要保护的东西，直接给 valid。modules 里的名字要和 extension.Enterprise
的 license_module 对上。
"""

from knowwork_server.extensions import LicenseState

MODULE = "enterprise"


class Verifier:
    def verify(self) -> LicenseState:
        return LicenseState(status="valid", message="自研扩展（内部使用）", modules=frozenset({MODULE}))
