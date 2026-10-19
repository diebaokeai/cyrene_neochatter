"""cyrene_neochatter —— 以 F:\昔涟agent 为底的独立私聊引擎插件。

自包含（自带 persona/WorldBook/分层记忆），不依赖 cyrene / dfc / nfc。
模型调用走 MoFox API（embedding/LLM/可选 rerank），并预留本地推理接口位。
默认关闭（general.enabled=false），开启后仅接管私聊。
"""

__all__: list[str] = []
