"""Typisierte Schlüssel für app[...] (aiohttp AppKey)."""
from aiohttp import web

SETTINGS = web.AppKey("nfizyo_settings", object)
DB = web.AppKey("nfizyo_db", object)
I18N = web.AppKey("nfizyo_i18n", object)
MODULES = web.AppKey("nfizyo_modules", dict)
AI = web.AppKey("nfizyo_ai", object)
THROTTLE = web.AppKey("nfizyo_throttle", object)
BADGES = web.AppKey("nfizyo_badges", dict)
BG_TASKS = web.AppKey("nfizyo_bg_tasks", set)
SMS = web.AppKey("nfizyo_sms", object)
MAIL = web.AppKey("nfizyo_mail", object)
JOBS = web.AppKey("nfizyo_jobs", object)
STATIC_VERSION = web.AppKey("nfizyo_static_version", str)
HIN = web.AppKey("nfizyo_hin", object)
