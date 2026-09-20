"""知识库视频语音转写。"""

from api.services.knowledge.asr.factory import get_asr_provider

__all__ = ["get_asr_provider"]
