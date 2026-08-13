"""不得通过资产网关或站内代码浏览暴露的敏感文件名。"""

SENSITIVE_FILE_NAMES: frozenset[str] = frozenset(
    {
        ".npmrc",
        ".pypirc",
        ".sandbox-secrets",
        "auth.json",
        "credentials.json",
        "id_dsa",
        "id_ed25519",
        "id_rsa",
        "secrets.json",
        "token.json",
    }
)
