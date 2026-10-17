"""
SmartAgro Modular Routes Package
--------------------------------
Contains Flask Blueprints for:
- weather: Live weather, Visual Crossing extended forecast, Sentinel-2 NDVI, crop recommendations
- market: Mandi APMC prices (Agmarknet), MSP rates, market translations
- diagnose: AI crop disease vision ensembling, Gemini Pro cross-checking, QA audit review
- chat: Kisan Helper multilingual assistant, Groq Whisper voice STT, intent gateway
- alerts: Rule & AI-driven weather alerts, crop risks, seasonal guidance, multilingual dashboard translations
"""

from .weather import weather_bp
from .market import market_bp
from .diagnose import diagnose_bp
from .chat import chat_bp
from .alerts import alerts_bp

__all__ = ["weather_bp", "market_bp", "diagnose_bp", "chat_bp", "alerts_bp"]
