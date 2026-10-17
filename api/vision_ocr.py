# -*- coding: utf-8 -*-
"""
外部 AI 视觉模型 OCR 模块

支持多种 AI 视觉模型进行图片文字/公式识别：
- OpenAI GPT-4o / GPT-4-vision
- Claude 3 Vision (Anthropic)
- 通义千问 VL (Qwen-VL)
- 硅基流动 (SiliconFlow) 视觉模型
- DeepSeek（deepseek-flash，实测能读公式图，见 PROVIDER_DEFAULTS 注释）
- 其他 OpenAI 兼容 API

通过环境变量配置：
- CHAOXING_VISION_OCR_PROVIDER: 提供商类型 (openai, claude, qwen, siliconflow, deepseek, openai_compatible)
- CHAOXING_VISION_OCR_ENDPOINT: API 端点 (可选，各提供商有默认值)
- CHAOXING_VISION_OCR_KEY: API 密钥
- CHAOXING_VISION_OCR_MODEL: 模型名称 (可选，各提供商有默认值)
- CHAOXING_VISION_OCR_PROMPT: 自定义 OCR 提示词 (可选)
"""

import base64
import os
import threading
from typing import Optional, Dict, Any

import requests

from api.logger import logger

# ============== 配置常量 ==============

# 默认 OCR 提示词 - 严格模式，专为题目识别优化
DEFAULT_OCR_PROMPT = """你是一个专业的 OCR 文字识别引擎。请严格按照以下规则识别图片中的内容：

【核心规则】
1. 只输出图片中的原始文字内容，禁止添加任何解释、描述、评论或额外文字
2. 禁止输出"图片中包含..."、"这是..."、"识别结果..."等引导语
3. 禁止对内容进行翻译、改写或补充

【数学公式处理】
- 数学公式必须用 LaTeX 格式输出
- 行内公式用单个 $ 包裹，如：$x^2 + y^2 = r^2$
- 独立公式用 $$ 包裹，如：$$\\int_a^b f(x)dx$$
- 分数用 \\frac{分子}{分母}
- 根号用 \\sqrt{} 或 \\sqrt[n]{}
- 希腊字母用对应命令：α→\\alpha, β→\\beta, π→\\pi 等
- 求和用 \\sum，积分用 \\int，极限用 \\lim

【输出格式】
- 保持原文的阅读顺序（从上到下、从左到右）
- 多行内容用换行符分隔
- 如果图片为空白或无法识别任何文字，仅输出：[空]

现在请识别图片内容："""

# 提供商默认配置
PROVIDER_DEFAULTS: Dict[str, Dict[str, str]] = {
    "openai": {
        "endpoint": "https://api.openai.com/v1/chat/completions",
        "model": "gpt-4o",
    },
    "claude": {
        "endpoint": "https://api.anthropic.com/v1/messages",
        "model": "claude-3-5-sonnet-20241022",
    },
    "qwen": {
        "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        "model": "qwen-vl-plus",
    },
    "siliconflow": {
        "endpoint": "https://api.siliconflow.cn/v1/chat/completions",
        "model": "Qwen/Qwen2-VL-72B-Instruct",
    },
    # DeepSeek 官方 API 也能读图（2026-10-17 实测）：
    #   GET  /models                        -> ['deepseek-flash', 'deepseek-v4-pro']
    #   带 image_url 的 /chat/completions    -> 200，把 $F(x)$ 分段函数与 "k =" 的公式图
    #                                          正确识别成 LaTeX，1.8~2.2 秒/张、42 tokens
    # 注意：它是推理模型，默认会先输出 reasoning_content；_call_openai_compatible 对
    # deepseek 端点自动带上 thinking={"type": "disabled"}，否则 1024 tokens 全烧在
    # 思维链上、content 返回空串（实测过）。所以这里只给默认 endpoint/model。
    "deepseek": {
        "endpoint": "https://api.deepseek.com/chat/completions",
        "model": "deepseek-flash",
    },
    "openai_compatible": {
        "endpoint": "",  # 必须用户指定
        "model": "gpt-4o",
    },
}

# ============== 全局状态 ==============

_VISION_OCR_ENABLED: Optional[bool] = None
_VISION_OCR_CONFIG: Optional[Dict[str, str]] = None
_VISION_OCR_LOCK = threading.Lock()


def _load_vision_ocr_config() -> Optional[Dict[str, str]]:
    """从环境变量加载视觉 OCR 配置"""
    global _VISION_OCR_ENABLED, _VISION_OCR_CONFIG

    with _VISION_OCR_LOCK:
        if _VISION_OCR_ENABLED is not None:
            return _VISION_OCR_CONFIG if _VISION_OCR_ENABLED else None

        provider = os.environ.get("CHAOXING_VISION_OCR_PROVIDER", "").strip().lower()
        api_key = os.environ.get("CHAOXING_VISION_OCR_KEY", "").strip()

        if not provider or not api_key:
            _VISION_OCR_ENABLED = False
            _VISION_OCR_CONFIG = None
            return None

        # 获取提供商默认配置
        defaults = PROVIDER_DEFAULTS.get(provider, PROVIDER_DEFAULTS["openai_compatible"])

        endpoint = os.environ.get("CHAOXING_VISION_OCR_ENDPOINT", "").strip()
        if not endpoint:
            endpoint = defaults["endpoint"]

        if not endpoint:
            logger.warning(f"视觉 OCR 提供商 '{provider}' 需要指定 CHAOXING_VISION_OCR_ENDPOINT")
            _VISION_OCR_ENABLED = False
            _VISION_OCR_CONFIG = None
            return None

        model = os.environ.get("CHAOXING_VISION_OCR_MODEL", "").strip()
        if not model:
            model = defaults["model"]

        prompt = os.environ.get("CHAOXING_VISION_OCR_PROMPT", "").strip()
        if not prompt:
            prompt = DEFAULT_OCR_PROMPT

        _VISION_OCR_CONFIG = {
            "provider": provider,
            "endpoint": endpoint,
            "api_key": api_key,
            "model": model,
            "prompt": prompt,
        }
        _VISION_OCR_ENABLED = True
        logger.info(f"外部 AI 视觉 OCR 已启用: provider={provider}, model={model}")
        return _VISION_OCR_CONFIG


def _image_to_base64(image_bytes: bytes) -> str:
    """将图片字节转换为 base64 字符串"""
    return base64.b64encode(image_bytes).decode("utf-8")


def _detect_image_type(image_bytes: bytes) -> str:
    """检测图片类型"""
    if image_bytes[:8] == b'\x89PNG\r\n\x1a\n':
        return "image/png"
    elif image_bytes[:2] == b'\xff\xd8':
        return "image/jpeg"
    elif image_bytes[:6] in (b'GIF87a', b'GIF89a'):
        return "image/gif"
    elif image_bytes[:4] == b'RIFF' and image_bytes[8:12] == b'WEBP':
        return "image/webp"
    else:
        return "image/png"  # 默认


def _call_openai_compatible(config: Dict[str, str], image_bytes: bytes) -> str:
    """调用 OpenAI 兼容 API（包括 OpenAI、硅基流动、通义千问等）"""
    image_base64 = _image_to_base64(image_bytes)
    image_type = _detect_image_type(image_bytes)

    headers = {
        "Authorization": f"Bearer {config['api_key']}",
        "Content-Type": "application/json",
    }

    # DeepSeek V4 默认开启 thinking，OCR 这种纯转录任务不需要思维链，
    # 开启只会拖慢速度并抬高成本。仅对 DeepSeek 端点关闭。
    thinking_off = "deepseek" in (config.get("endpoint") or "").lower()

    payload = {
        "model": config["model"],
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": config["prompt"]},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{image_type};base64,{image_base64}"
                        }
                    }
                ]
            }
        ],
        "max_tokens": 1024,
        "temperature": 0.1,
    }

    if thinking_off:
        payload["thinking"] = {"type": "disabled"}
        # 个别兼容端点不认 thinking 字段，会直接 400；此时退回不带该字段重试一次。
        _thinking_fallback = True
    else:
        _thinking_fallback = False

    try:
        resp = requests.post(
            config["endpoint"],
            headers=headers,
            json=payload,
            timeout=60
        )
        if resp.status_code == 400 and _thinking_fallback:
            logger.debug("端点不接受 thinking 字段，改为普通请求重试")
            payload.pop("thinking", None)
            resp = requests.post(
                config["endpoint"],
                headers=headers,
                json=payload,
                timeout=60
            )
        if resp.status_code != 200:
            logger.debug(f"OpenAI 兼容 API 返回异常: {resp.status_code} - {resp.text[:200]}")
            return ""

        data = resp.json()
        # 解析响应
        choices = data.get("choices", [])
        if choices:
            choice = choices[0]
            message = choice.get("message", {})
            content = message.get("content", "")
            if content:
                content = content.strip()
                # 过滤掉空白标记和无效响应
                if content in ("[空]", "无文字内容", "[空白]", ""):
                    return ""
                return content
            # content 为空：多半是推理模型把额度都花在思维链上了，留下线索便于排查
            reasoning = message.get("reasoning_content") or ""
            if reasoning:
                logger.debug(
                    "OCR 响应只有思维过程没有正文（finish_reason=%s，思维链 %d 字）：模型可能不支持读图或 thinking 未关闭",
                    choice.get("finish_reason"), len(reasoning))
        return ""
    except Exception as exc:
        logger.debug(f"OpenAI 兼容 API 调用失败: {exc}")
        return ""


def _call_claude(config: Dict[str, str], image_bytes: bytes) -> str:
    """调用 Claude API (Anthropic)"""
    image_base64 = _image_to_base64(image_bytes)
    image_type = _detect_image_type(image_bytes)

    headers = {
        "x-api-key": config["api_key"],
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
    }

    payload = {
        "model": config["model"],
        "max_tokens": 1024,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": image_type,
                            "data": image_base64,
                        }
                    },
                    {"type": "text", "text": config["prompt"]}
                ]
            }
        ]
    }

    try:
        resp = requests.post(
            config["endpoint"],
            headers=headers,
            json=payload,
            timeout=30
        )
        if resp.status_code != 200:
            logger.debug(f"Claude API 返回异常: {resp.status_code} - {resp.text[:200]}")
            return ""

        data = resp.json()
        # 解析 Claude 响应格式
        content_blocks = data.get("content", [])
        for block in content_blocks:
            if block.get("type") == "text":
                text = block.get("text", "")
                if text:
                    text = text.strip()
                    # 过滤掉空白标记和无效响应
                    if text in ("[空]", "无文字内容", "[空白]", ""):
                        return ""
                    return text
        return ""
    except Exception as exc:
        logger.debug(f"Claude API 调用失败: {exc}")
        return ""


def vision_ocr(image_bytes: bytes) -> str:
    """使用外部 AI 视觉模型进行 OCR 识别

    Args:
        image_bytes: 图片的二进制数据

    Returns:
        识别出的文字内容，失败时返回空字符串
    """
    config = _load_vision_ocr_config()
    if not config:
        return ""

    provider = config["provider"]

    if provider == "claude":
        return _call_claude(config, image_bytes)
    else:
        # openai, qwen, siliconflow, openai_compatible 都使用 OpenAI 兼容格式
        return _call_openai_compatible(config, image_bytes)


def is_vision_ocr_enabled() -> bool:
    """检查外部 AI 视觉 OCR 是否已启用"""
    config = _load_vision_ocr_config()
    return config is not None


def reset_vision_ocr_config():
    """重置视觉 OCR 配置（用于测试或重新加载配置）"""
    global _VISION_OCR_ENABLED, _VISION_OCR_CONFIG
    with _VISION_OCR_LOCK:
        _VISION_OCR_ENABLED = None
        _VISION_OCR_CONFIG = None


_ENV_KEYS = (
    "CHAOXING_VISION_OCR_PROVIDER",
    "CHAOXING_VISION_OCR_KEY",
    "CHAOXING_VISION_OCR_ENDPOINT",
    "CHAOXING_VISION_OCR_MODEL",
)


def apply_config_env(provider: str, api_key: str, endpoint: str = "",
                     model: str = "") -> bool:
    """把设置页里的 OCR 配置写进环境变量，并让缓存失效；返回是否「有配置」。

    以前 app.py 里这段是「只写不删」：用户后来把 OCR 关掉或改回默认地址，
    进程里仍留着上一次的 endpoint/model，界面显示「未启用」而实际还在用旧配置。
    所以这里每次都先把四个变量清空，再按传入值写回。
    """
    for key in _ENV_KEYS:
        os.environ.pop(key, None)
    provider = (provider or "").strip()
    api_key = (api_key or "").strip()
    enabled = bool(provider and api_key)
    if enabled:
        os.environ["CHAOXING_VISION_OCR_PROVIDER"] = provider
        os.environ["CHAOXING_VISION_OCR_KEY"] = api_key
        if (endpoint or "").strip():
            os.environ["CHAOXING_VISION_OCR_ENDPOINT"] = endpoint.strip()
        if (model or "").strip():
            os.environ["CHAOXING_VISION_OCR_MODEL"] = model.strip()
    reset_vision_ocr_config()
    return enabled
