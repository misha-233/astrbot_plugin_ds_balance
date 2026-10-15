# -*- coding: utf-8 -*-
"""DeepSeek 余额查询与低额人格化提醒。

功能：
1. LLM 工具 `check_deepseek_balance`：机器人可自主查询 DeepSeek 账户余额
2. 后台定时检查：余额低于阈值时，用当前人格生成提醒，推送到白名单会话
3. `/余额` 命令：手动查询；`/余额 测试` 预览人格化提醒文案

说明：
- 复用 AstrBot 已有的 DeepSeek 密钥（provider_sources），无需重复填写
- 提醒目标留空时自动发给管理员私聊（读取 admins_id）
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_ds_balance"
ASTRBOT_CONFIG = Path("/AstrBot/data/cmd_config.json")
PERSONA_FALLBACK = "你是一个群聊机器人。"
STARTUP_DELAY = 60  # 秒：等平台适配器连接完成后再开始检查


@register(PLUGIN_NAME, "dsh", "DeepSeek 余额查询与低额提醒", "1.0.0")
class DSBalancePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self._loop_task: asyncio.Task | None = None
        self._last_remind_ts = self._load_last_remind()

    # ------------------------------------------------------------------ 配置
    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            if hasattr(self.config, "get"):
                val = self.config.get(key, default)
                if val is not None and val != "":
                    return val
        except Exception:
            pass
        return default

    def _state_file(self) -> Path:
        try:
            from astrbot.api.star import StarTools

            return Path(StarTools.get_data_dir(PLUGIN_NAME)) / "state.json"
        except Exception:
            return Path(__file__).parent / "state.json"

    def _load_last_remind(self) -> float:
        try:
            p = self._state_file()
            if p.exists():
                return float(json.loads(p.read_text("utf-8")).get("last_remind_ts") or 0)
        except Exception:
            pass
        return 0.0

    def _save_last_remind(self, ts: float) -> None:
        self._last_remind_ts = ts
        try:
            p = self._state_file()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"last_remind_ts": ts}), "utf-8")
        except Exception as e:
            logger.warning(f"[DSBalance] 保存提醒时间失败: {e}")

    # --------------------------------------------------- AstrBot 全局配置读取
    def _astrbot_config(self) -> dict:
        try:
            return json.loads(ASTRBOT_CONFIG.read_bytes().decode("utf-8-sig"))
        except Exception as e:
            logger.error(f"[DSBalance] 读取 AstrBot 配置失败: {e}")
            return {}

    def _provider_settings(self) -> dict:
        ps = self._astrbot_config().get("provider_settings") or {}
        return ps if isinstance(ps, dict) else {}

    def _platform_id(self) -> str:
        for p in self._astrbot_config().get("platform") or []:
            if isinstance(p, dict) and p.get("id") and p.get("enable", True):
                return str(p["id"])
        return "default"

    def _target_sessions(self) -> list[str]:
        """提醒推送白名单。

        支持三种写法：完整会话 ID、纯群号（按群聊解析）。
        另可开启 also_notify_admin，额外把管理员私聊加入目标（自动派生，不写入配置）。
        """
        raw = self._cfg("target_sessions", []) or []
        if isinstance(raw, str):
            raw = [x.strip() for x in raw.replace(",", "\n").splitlines() if x.strip()]

        pid = self._platform_id()
        out: list[str] = []
        for item in raw:
            s = str(item).strip()
            if not s:
                continue
            if ":" in s:
                out.append(s)
            elif s.isdigit():
                out.append(f"{pid}:GroupMessage:{s}")  # 纯数字按群号处理

        # 白名单为空 或 显式开启 also_notify_admin → 追加管理员私聊
        if not out or self._cfg("also_notify_admin", True):
            for admin in self._astrbot_config().get("admins_id") or []:
                a = str(admin).strip()
                if a.isdigit():
                    sess = f"{pid}:FriendMessage:{a}"
                    if sess not in out:
                        out.append(sess)
        return out

    @staticmethod
    def _extract_key(raw: Any) -> str:
        """AstrBot 的 provider_sources.key 可能是字符串，也可能是只含一个字符串的列表。"""
        if isinstance(raw, (list, tuple)):
            raw = raw[0] if raw else ""
        return str(raw or "").strip()

    def _list_sources(self) -> list[dict]:
        """列出所有配了 key 的 DeepSeek 账号（复用 AstrBot 已有的密钥）。"""
        out: list[dict] = []
        for s in self._astrbot_config().get("provider_sources") or []:
            if not isinstance(s, dict):
                continue
            key = self._extract_key(s.get("key"))
            base = str(s.get("api_base") or "")
            prov = str(s.get("provider") or "")
            if not key:
                continue
            if "deepseek" in base.lower() or prov.lower() == "deepseek":
                out.append({"id": str(s.get("id") or "?"), "key": key, "api_base": base})
        return out

    # ---------------------------------------------------------------- 余额查询
    @staticmethod
    def _balance_url(api_base: str) -> str:
        base = (api_base or "https://api.deepseek.com/v1").rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        return base + "/user/balance"

    async def _fetch(self, source: dict) -> dict | None:
        url = self._balance_url(source.get("api_base", ""))
        timeout = float(self._cfg("request_timeout", 20) or 20)
        try:
            async with httpx.AsyncClient(timeout=timeout) as cli:
                r = await cli.get(url, headers={"Authorization": f"Bearer {source['key']}"})
                r.raise_for_status()
                return r.json()
        except Exception as e:
            logger.warning(f"[DSBalance] 查询 {source.get('id')} 余额失败: {e}")
            return None

    @staticmethod
    def _parse(account_id: str, data: dict) -> tuple[str, float | None]:
        infos = data.get("balance_infos") or []
        avail = "可用" if data.get("is_available") else "不可用"
        if not infos:
            return f"【{account_id}】{avail}，无余额明细", None
        segs: list[str] = []
        cny: float | None = None
        for info in infos:
            cur = info.get("currency", "?")
            segs.append(
                f"{cur} {info.get('total_balance', '?')}"
                f"（赠金 {info.get('granted_balance', '?')}"
                f" / 充值 {info.get('topped_up_balance', '?')}）"
            )
            if cur == "CNY":
                try:
                    cny = float(info.get("total_balance"))
                except (TypeError, ValueError):
                    pass
        return f"【{account_id}】{avail}，" + "；".join(segs), cny

    async def _query_all(self, account: str = "") -> tuple[list[str], float | None, str]:
        """返回 (文本行, 最低 CNY 余额, 最低余额所在账号)。"""
        sources = self._list_sources()
        if not sources:
            return ["未找到已配置的 DeepSeek 账号（provider_sources 中缺少 key）。"], None, ""
        if account:
            picked = [s for s in sources if account in s["id"]]
            if not picked:
                ids = "、".join(s["id"] for s in sources)
                return [f"未找到账号「{account}」。可用账号：{ids}"], None, ""
            sources = picked

        lines: list[str] = []
        lowest: float | None = None
        lowest_id = ""
        for s in sources:
            data = await self._fetch(s)
            if not data:
                lines.append(f"【{s['id']}】查询失败")
                continue
            text, cny = self._parse(s["id"], data)
            lines.append(text)
            if cny is not None and (lowest is None or cny < lowest):
                lowest, lowest_id = cny, s["id"]
        return lines, lowest, lowest_id

    # -------------------------------------------------------- 人格化提醒生成
    async def _persona_text(self) -> str:
        """取人格人设文本（用 AstrBot 公开 API）。"""
        pid = str(self._cfg("persona_name", "") or "") or str(
            self._provider_settings().get("default_personality") or ""
        )
        try:
            persona = None
            if pid:
                persona = await self.context.persona_manager.get_persona(pid)
            if not persona:
                all_p = await self.context.persona_manager.get_all_personas()
                persona = all_p[0] if all_p else None
            if persona:
                return (
                    getattr(persona, "system_prompt", None)
                    or getattr(persona, "prompt", None)
                    or PERSONA_FALLBACK
                )
        except Exception as e:
            logger.warning(f"[DSBalance] 获取人格失败: {e}")
        return PERSONA_FALLBACK

    async def _make_reminder(self, account: str, balance: float) -> str:
        threshold = float(self._cfg("threshold", 10) or 10)
        fallback = (
            f"⚠️ DeepSeek 余额提醒：{account} 仅剩 {balance} 元"
            f"（阈值 {threshold} 元），记得充值。"
        )
        if not self._cfg("use_persona", True):
            return fallback

        persona = await self._persona_text()
        provider_id = str(self._cfg("chat_provider_id", "") or "") or str(
            self._provider_settings().get("default_provider_id") or ""
        )
        if not provider_id:
            logger.warning("[DSBalance] 未找到可用 provider，退回模板文案")
            return fallback

        instruction = self._cfg(
            "remind_instruction",
            "你正在主动发一条消息——不是回复谁，而是你自己突然想说起这件事。"
            "请用你平时的语气把它随口讲出来，可以带点情绪。",
        )
        prompt = (
            f"{instruction}\n\n"
            f"背景：账号 {account} 的余额只剩 {balance} 元，已经低于 {threshold} 元的警戒线。"
        )
        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=persona,
            )
            text = (getattr(resp, "completion_text", "") or "").strip()
            return text or fallback
        except Exception as e:
            logger.warning(f"[DSBalance] 生成人格化提醒失败，退回模板: {e}")
            return fallback

    # -------------------------------------------------------------- LLM 工具
    @filter.llm_tool(name="check_deepseek_balance")
    async def check_deepseek_balance(self, event: AstrMessageEvent, account: str = ""):
        """查询 DeepSeek API 账户余额（还剩多少钱、还有多少额度）。

        何时使用：用户问你还剩多少余额、额度、钱够不够、是不是快没钱了、还能不能继续用的时候。

        Args:
            account(string): 可选。指定账号 id，例如 "deepseek" 或 "deepseek_1"。留空则查询全部已配置账号。
        """
        lines, lowest, lowest_id = await self._query_all(account)
        result = "\n".join(lines)
        if lowest is not None:
            threshold = float(self._cfg("threshold", 10) or 10)
            if lowest < threshold:
                result += f"\n（注意：{lowest_id} 余额 {lowest} 元已低于警戒线 {threshold} 元）"
        return result

    # ------------------------------------------------------------------ 命令
    @filter.command("余额", alias={"balance"})
    async def cmd_balance(self, event: AstrMessageEvent, arg: str = ""):
        """查询 DeepSeek 余额。用法：/余额 | /余额 deepseek_1 | /余额 测试"""
        arg = (arg or "").strip()
        if arg in ("测试", "test"):
            _, lowest, lowest_id = await self._query_all("")
            bal = lowest if lowest is not None else 0.0
            text = await self._make_reminder(lowest_id or "deepseek", bal)
            targets = self._target_sessions()
            yield event.plain_result(
                f"【提醒文案预览】\n{text}\n\n【推送白名单】\n" + "\n".join(targets or ["（空）"])
            )
            return
        lines, _, _ = await self._query_all(arg)
        yield event.plain_result("\n".join(lines))

    # -------------------------------------------------------------- 后台循环
    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.create_task(self._loop())
            logger.info(f"[DSBalance] 后台余额检查已启动（{STARTUP_DELAY}s 后首次检查）")

    async def _loop(self):
        await asyncio.sleep(STARTUP_DELAY)  # 等平台适配器连接完成
        while True:
            try:
                await self._check_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[DSBalance] 检查余额异常: {e}", exc_info=True)
            hours = max(0.1, float(self._cfg("check_interval_hours", 6) or 6))
            await asyncio.sleep(hours * 3600)

    async def _check_once(self):
        if not self._cfg("enabled", True):
            return
        threshold = float(self._cfg("threshold", 10) or 10)
        _, lowest, lowest_id = await self._query_all("")
        if lowest is None or lowest >= threshold:
            return
        cooldown = float(self._cfg("remind_cooldown_hours", 24) or 24) * 3600
        if time.time() - self._last_remind_ts < cooldown:
            logger.info("[DSBalance] 余额低但仍在提醒冷却期内，跳过")
            return

        targets = self._target_sessions()
        if not targets:
            logger.warning("[DSBalance] 提醒白名单为空且未取到管理员 ID，跳过提醒")
            return

        text = await self._make_reminder(lowest_id, lowest)
        sent_any = False
        for session in targets:
            try:
                ok = await self.context.send_message(session, MessageChain([Plain(text)]))
                if ok:
                    sent_any = True
                    logger.info(f"[DSBalance] 已推送低额提醒到 {session}: {text}")
                else:
                    logger.warning(f"[DSBalance] 推送失败（找不到平台/会话）: {session}")
            except Exception as e:
                logger.error(f"[DSBalance] 推送到 {session} 异常: {e}", exc_info=True)

        if sent_any:
            self._save_last_remind(time.time())
            logger.info(f"[DSBalance] 低额提醒完成（{lowest_id}={lowest} < {threshold}）")
        else:
            logger.warning("[DSBalance] 全部目标推送失败，不计入冷却，下轮重试")

    async def terminate(self):
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
