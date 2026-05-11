#!/usr/bin/env python3
"""
Cap Bot — Telegram бот для проверки кап байерами
Флоу:
  1. /start → бот спрашивает тег (например tetriss_mb)
  2. Находит Notion User ID из маппинга BUYERS мгновенно
  3. Проверяет группу в Keitaro
  4. 🔍 Проверить капы → отчёт по всем потокам
  5. Кнопки Стоп / Холд → меняет статус в Notion
"""

import os, re, json, time, logging, requests, sys
from datetime import datetime, timedelta

# ─── КОНФИГ ───────────────────────────────────────────────────────────────────
def require_env(name):
    val = os.environ.get(name)
    if not val:
        print(f"❌ Переменная окружения {name} не задана!")
        sys.exit(1)
    return val

KEITARO_URL   = require_env("KEITARO_URL")
KEITARO_KEY   = require_env("KEITARO_KEY")
NOTION_TOKEN  = require_env("NOTION_TOKEN")
NOTION_DB_ID  = require_env("NOTION_DB_ID")
TG_BOT_TOKEN  = require_env("TG_BOT_TOKEN")

# ─── МАППИНГ ТЕГ → NOTION USER ID ────────────────────────────────────────────
# Чтобы добавить байера — добавь строку: "тег": "notion-uuid"
BUYERS = {
    "tetriss_mb": "561afa0f-0a44-4221-acda-e2f8f3e98e2e",
}
# ──────────────────────────────────────────────────────────────────────────────

USERS_FILE      = "cap_bot_users.json"
NO_TRAFFIC_DAYS = 7

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("cap_bot.log", encoding="utf-8"),
    ]
)
log = logging.getLogger(__name__)

# ─── ХРАНИЛИЩЕ ПОЛЬЗОВАТЕЛЕЙ ──────────────────────────────────────────────────
def load_users():
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_users(users):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2, ensure_ascii=False)

# ─── NOTION ───────────────────────────────────────────────────────────────────
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

def resolve_buyer(tag: str):
    """Находит notion_id и имя байера по тегу из маппинга. Мгновенно."""
    tag = tag.strip().lower()
    for buyer_tag, notion_id in BUYERS.items():
        if tag == buyer_tag.lower():
            return {"id": notion_id, "name": buyer_tag}
    return None

def get_streams_for_buyer(buyer_notion_id: str):
    url = f"https://api.notion.com/v1/databases/{NOTION_DB_ID}/query"
    payload = {
        "filter": {
            "and": [
                {
                    "or": [
                        {"property": "Баер статус", "select": {"equals": "Запущен"}},
                        {"property": "Баер статус", "select": {"equals": "Не запущен"}},
                        {"property": "Баер статус", "select": {"equals": "Холд"}},
                    ]
                },
                {"property": "Ответственный", "people": {"contains": buyer_notion_id}}
            ]
        }
    }
    streams = []
    while True:
        r = requests.post(url, headers=NOTION_HEADERS, json=payload, timeout=15)
        r.raise_for_status()
        data = r.json()
        for page in data["results"]:
            props = page["properties"]
            ln_id = ""
            for key in ["userDefined:ID", "ID", ""]:
                p = props.get(key, {})
                if p.get("type") == "title" and p.get("title"):
                    ln_id = p["title"][0]["plain_text"].strip()
                    break
            if not re.match(r"^LN-\d+$", ln_id):
                continue
            cap_raw = ""
            cap_prop = props.get("Cap", {})
            if cap_prop.get("type") == "rich_text" and cap_prop.get("rich_text"):
                cap_raw = cap_prop["rich_text"][0]["plain_text"]
            cap_num = parse_cap(cap_raw)
            if not cap_num:
                continue
            status = ""
            st_prop = props.get("Баер статус", {})
            if st_prop.get("select"):
                status = st_prop["select"]["name"]
            streams.append({
                "notion_id":  page["id"],
                "ln_id":      ln_id,
                "cap":        cap_num,
                "status":     status,
                "notion_url": page["url"],
            })
        if not data.get("has_more"):
            break
        payload["start_cursor"] = data["next_cursor"]
    return streams

def parse_cap(raw: str) -> int:
    nums = re.findall(r"\d+", raw)
    return int(nums[-1]) if nums else 0

def set_notion_status(page_id: str, status: str):
    url = f"https://api.notion.com/v1/pages/{page_id}"
    payload = {"properties": {"Баер статус": {"select": {"name": status}}}}
    r = requests.patch(url, headers=NOTION_HEADERS, json=payload, timeout=15)
    r.raise_for_status()

# ─── KEITARO ──────────────────────────────────────────────────────────────────
def get_campaigns_by_group(group_name: str):
    """
    Получает все кампании из Keitaro.
    Фильтрация по байеру не нужна — Notion уже отдал только его потоки.
    Матчинг идёт по LN-XXXXX в названии кампании.
    """
    headers = {"Api-Key": KEITARO_KEY}
    r = requests.get(f"{KEITARO_URL}/admin_api/v1/campaigns", headers=headers, timeout=15)
    r.raise_for_status()
    all_camps = r.json()
    log.info(f"Всего кампаний в Keitaro: {len(all_camps)}")
    return all_camps

def get_stats(campaign_ids, days=90):
    if not campaign_ids:
        return {}
    url = f"{KEITARO_URL}/admin_api/v1/report/build"
    headers = {"Api-Key": KEITARO_KEY, "Content-Type": "application/json"}
    date_from = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    date_to   = datetime.now().strftime("%Y-%m-%d")
    payload = {
        "range": {"from": date_from, "to": date_to, "timezone": "Europe/Kyiv"},
        "filters": [{"name": "campaign_id", "operator": "IN", "expression": [str(i) for i in campaign_ids]}],
        "grouping": ["campaign_id"],
        "metrics": ["sales", "deposits", "cost"],
    }
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    result = {}
    for row in r.json().get("rows", []):
        cid = int(row.get("campaign_id", 0))
        result[cid] = {
            "sales":    int(row.get("sales",    0) or 0),
            "deposits": int(row.get("deposits", 0) or 0),
            "cost":     float(row.get("cost",   0) or 0),
        }
    return result

# ─── TELEGRAM ─────────────────────────────────────────────────────────────────
def tg_api(method: str, **kwargs):
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/{method}"
    r = requests.post(url, json=kwargs, timeout=15)
    r.raise_for_status()
    return r.json()

def tg_send(chat_id, text: str, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg_api("sendMessage", **payload)

def tg_edit(chat_id, message_id, text: str, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        tg_api("editMessageText", **payload)
    except:
        pass

def answer_callback(callback_query_id: str, text: str = ""):
    tg_api("answerCallbackQuery", callback_query_id=callback_query_id, text=text)

def main_keyboard():
    return {
        "keyboard": [
            [{"text": "🔍 Проверить капы"}],
            [{"text": "⚙️ Изменить настройки"}],
        ],
        "resize_keyboard": True,
    }

# ─── ПРОВЕРКА КАП ─────────────────────────────────────────────────────────────
def check_caps_for_user(buyer_notion_id: str, keitaro_group: str):
    streams = get_streams_for_buyer(buyer_notion_id)
    if not streams:
        return "📭 Потоков не найдено (статус Запущен/Не запущен/Холд)", []

    campaigns = get_campaigns_by_group(keitaro_group)
    ln_to_cids = {}
    for c in campaigns:
        m = re.match(r"^(LN-\d+)", c.get("name", ""))
        if m:
            ln = m.group(1)
            ln_to_cids.setdefault(ln, []).append(int(c["id"]))

    all_cids = [cid for s in streams for cid in ln_to_cids.get(s["ln_id"], [])]
    stats_90d = get_stats(all_cids, days=90)
    stats_7d  = get_stats(all_cids, days=NO_TRAFFIC_DAYS)

    lines = [f"📊 <b>Отчёт по капам</b> — {keitaro_group}\n"]
    actions = []

    for s in streams:
        ln_id = s["ln_id"]
        cap   = s["cap"]
        cids  = ln_to_cids.get(ln_id, [])

        if not cids:
            lines.append(f"⚪ <b>{ln_id}</b> — нет в Keitaro")
            continue

        total_90d = sum(stats_90d.get(c, {}).get("sales", 0) + stats_90d.get(c, {}).get("deposits", 0) for c in cids)
        cost_7d   = sum(stats_7d.get(c, {}).get("cost", 0) for c in cids)
        remaining = cap - total_90d
        pct       = total_90d / cap if cap else 0

        link = f'<a href="{s["notion_url"]}">{ln_id}</a>'

        if pct >= 1.0:
            overflow = total_90d - cap
            lines.append(f"🔴 {link} — <b>ПЕРЕЛИВ на {overflow} FD</b> ({total_90d}/{cap}) — нужен стоп!")
            actions.append({"ln_id": ln_id, "notion_id": s["notion_id"], "action": "stop"})
        elif cost_7d == 0:
            lines.append(f"⚠️ {link} — <b>не льётся {NO_TRAFFIC_DAYS} дней</b> → поставить холд?")
            actions.append({"ln_id": ln_id, "notion_id": s["notion_id"], "action": "hold"})
        elif remaining <= 0:
            lines.append(f"🟡 {link} — капа закрыта ({total_90d}/{cap})")
        else:
            lines.append(f"✅ {link} — ещё <b>{remaining} FD</b> ({total_90d}/{cap})")

    return "\n".join(lines), actions

def build_actions_keyboard(actions):
    buttons = []
    for a in actions:
        if a["action"] == "stop":
            buttons.append([{"text": f"⛔ Стоп {a['ln_id']}", "callback_data": f"stop:{a['notion_id']}:{a['ln_id']}"}])
        elif a["action"] == "hold":
            buttons.append([{"text": f"❄️ Холд {a['ln_id']}", "callback_data": f"hold:{a['notion_id']}:{a['ln_id']}"}])
    return {"inline_keyboard": buttons} if buttons else None

# ─── СОСТОЯНИЯ ────────────────────────────────────────────────────────────────
STATE_IDLE      = "idle"
STATE_ASK_TAG   = "ask_tag"

# ─── ОБРАБОТКА СООБЩЕНИЙ ──────────────────────────────────────────────────────
def handle_message(msg: dict, users: dict, states: dict):
    chat_id = str(msg["chat"]["id"])
    text    = msg.get("text", "").strip()
    state   = states.get(chat_id, STATE_IDLE)

    if text == "/start":
        user = users.get(chat_id)
        if user:
            tg_send(chat_id,
                f"👋 С возвращением, <b>{user['tag']}</b>!\n\n"
                f"Нажми кнопку чтобы проверить капы.",
                reply_markup=main_keyboard()
            )
        else:
            states[chat_id] = STATE_ASK_TAG
            tg_send(chat_id,
                "👋 Привет! Я бот для проверки кап.\n\n"
                "Введи свой тег (например: <code>tetriss_mb</code>):"
            )
        return

    if text == "⚙️ Изменить настройки":
        states[chat_id] = STATE_ASK_TAG
        tg_send(chat_id, "Введи свой тег:")
        return

    if text == "🔍 Проверить капы":
        user = users.get(chat_id)
        if not user:
            states[chat_id] = STATE_ASK_TAG
            tg_send(chat_id, "Сначала введи свой тег:")
            return
        tg_send(chat_id, "⏳ Загружаю данные...")
        try:
            report, actions = check_caps_for_user(user["notion_id"], user["tag"])
            keyboard = build_actions_keyboard(actions)
            tg_send(chat_id, report, reply_markup=keyboard)
        except Exception as e:
            log.error(f"check_caps error: {e}")
            tg_send(chat_id, f"❌ Ошибка: {e}")
        return

    if state == STATE_ASK_TAG:
        buyer = resolve_buyer(text)
        if not buyer:
            known = ", ".join(f"<code>{t}</code>" for t in BUYERS.keys())
            tg_send(chat_id,
                f"❌ Тег <b>{text}</b> не найден.\n"
                f"Доступные теги: {known}\n\n"
                f"Попробуй ещё раз:"
            )
            return
        users[chat_id] = {
            "tag":       buyer["name"],
            "notion_id": buyer["id"],
        }
        states[chat_id] = STATE_IDLE
        save_users(users)
        tg_send(chat_id,
            f"✅ Готово! Тег: <b>{buyer['name']}</b>\n\n"
            f"Нажми <b>🔍 Проверить капы</b>",
            reply_markup=main_keyboard()
        )
        return

    tg_send(chat_id, "Используй кнопки 👇", reply_markup=main_keyboard())


def handle_callback(cb: dict, users: dict):
    chat_id           = str(cb["message"]["chat"]["id"])
    message_id        = cb["message"]["message_id"]
    callback_query_id = cb["id"]
    data              = cb.get("data", "")

    parts = data.split(":", 2)
    if len(parts) != 3:
        answer_callback(callback_query_id, "Неизвестное действие")
        return

    action, notion_id, ln_id = parts
    status_map = {"stop": "Стопнут", "hold": "Холд"}
    emoji_map  = {"stop": "⛔", "hold": "❄️"}

    if action in status_map:
        try:
            set_notion_status(notion_id, status_map[action])
            answer_callback(callback_query_id, f"{emoji_map[action]} {ln_id} → {status_map[action]}")
            original_text = cb["message"].get("text", "")
            existing_buttons = cb["message"].get("reply_markup", {}).get("inline_keyboard", [])
            new_buttons = [
                row for row in existing_buttons
                if not any(btn.get("callback_data", "").startswith(f"{action}:{notion_id}") for btn in row)
            ]
            tg_edit(
                chat_id, message_id,
                original_text + f"\n\n✅ <b>{ln_id}</b> → <b>{status_map[action]}</b>",
                reply_markup={"inline_keyboard": new_buttons} if new_buttons else None
            )
        except Exception as e:
            answer_callback(callback_query_id, f"Ошибка: {e}")

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    log.info("🤖 Cap Bot запущен")
    users  = load_users()
    states = {}
    offset = 0

    while True:
        try:
            result  = tg_api("getUpdates", offset=offset, timeout=20, allowed_updates=["message", "callback_query"])
            updates = result.get("result", [])
            for upd in updates:
                offset = upd["update_id"] + 1
                if "message" in upd:
                    try:
                        handle_message(upd["message"], users, states)
                    except Exception as e:
                        log.error(f"handle_message error: {e}")
                elif "callback_query" in upd:
                    try:
                        handle_callback(upd["callback_query"], users)
                    except Exception as e:
                        log.error(f"handle_callback error: {e}")
        except requests.exceptions.Timeout:
            pass
        except Exception as e:
            log.error(f"polling error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
NO_TRAFFIC_DAYS = 7

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("cap_bot.log", encoding="utf-8"),
    ]
)
log = logging.getLogger(__name__)

# ─── ХРАНИЛИЩЕ ПОЛЬЗОВАТЕЛЕЙ ──────────────────────────────────────────────────
def load_users():
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_users(users):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2, ensure_ascii=False)

# ─── NOTION ───────────────────────────────────────────────────────────────────
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

def resolve_buyer(tag: str):
    """Находит notion_id и имя байера по тегу из маппинга. Мгновенно."""
    tag = tag.strip().lower()
    for buyer_tag, notion_id in BUYERS.items():
        if tag == buyer_tag.lower():
            return {"id": notion_id, "name": buyer_tag}
    return None

def get_streams_for_buyer(buyer_notion_id: str):
    url = f"https://api.notion.com/v1/databases/{NOTION_DB_ID}/query"
    payload = {
        "filter": {
            "and": [
                {
                    "or": [
                        {"property": "Баер статус", "select": {"equals": "Запущен"}},
                        {"property": "Баер статус", "select": {"equals": "Не запущен"}},
                        {"property": "Баер статус", "select": {"equals": "Холд"}},
                    ]
                },
                {"property": "Ответственный", "people": {"contains": buyer_notion_id}}
            ]
        }
    }
    streams = []
    while True:
        r = requests.post(url, headers=NOTION_HEADERS, json=payload, timeout=15)
        r.raise_for_status()
        data = r.json()
        for page in data["results"]:
            props = page["properties"]
            ln_id = ""
            for key in ["userDefined:ID", "ID", ""]:
                p = props.get(key, {})
                if p.get("type") == "title" and p.get("title"):
                    ln_id = p["title"][0]["plain_text"].strip()
                    break
            if not re.match(r"^LN-\d+$", ln_id):
                continue
            cap_raw = ""
            cap_prop = props.get("Cap", {})
            if cap_prop.get("type") == "rich_text" and cap_prop.get("rich_text"):
                cap_raw = cap_prop["rich_text"][0]["plain_text"]
            cap_num = parse_cap(cap_raw)
            if not cap_num:
                continue
            status = ""
            st_prop = props.get("Баер статус", {})
            if st_prop.get("select"):
                status = st_prop["select"]["name"]
            streams.append({
                "notion_id":  page["id"],
                "ln_id":      ln_id,
                "cap":        cap_num,
                "status":     status,
                "notion_url": page["url"],
            })
        if not data.get("has_more"):
            break
        payload["start_cursor"] = data["next_cursor"]
    return streams

def parse_cap(raw: str) -> int:
    nums = re.findall(r"\d+", raw)
    return int(nums[-1]) if nums else 0

def set_notion_status(page_id: str, status: str):
    url = f"https://api.notion.com/v1/pages/{page_id}"
    payload = {"properties": {"Баер статус": {"select": {"name": status}}}}
    r = requests.patch(url, headers=NOTION_HEADERS, json=payload, timeout=15)
    r.raise_for_status()

# ─── KEITARO ──────────────────────────────────────────────────────────────────
def get_campaigns_by_group(group_name: str):
    """
    Получает кампании из Keitaro и фильтрует по email аккаунта в названии.
    Название кампании: LN-13397; KR; Gambloria; T20; ; tetriss_mb@044.agency;
    group_name = тег байера (например tetriss_mb) — ищем его в названии.
    """
    headers = {"Api-Key": KEITARO_KEY}
    r = requests.get(f"{KEITARO_URL}/admin_api/v1/campaigns", headers=headers, timeout=15)
    r.raise_for_status()
    all_camps = r.json()
    tag = group_name.lower()
    filtered = [c for c in all_camps if tag in c.get("name", "").lower()]
    log.info(f"Кампаний для '{group_name}': {len(filtered)} из {len(all_camps)}")
    return filtered

def get_stats(campaign_ids, days=90):
    if not campaign_ids:
        return {}
    url = f"{KEITARO_URL}/admin_api/v1/report/build"
    headers = {"Api-Key": KEITARO_KEY, "Content-Type": "application/json"}
    date_from = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    date_to   = datetime.now().strftime("%Y-%m-%d")
    payload = {
        "range": {"from": date_from, "to": date_to, "timezone": "Europe/Kyiv"},
        "filters": [{"name": "campaign_id", "operator": "IN", "expression": [str(i) for i in campaign_ids]}],
        "grouping": ["campaign_id"],
        "metrics": ["sales", "deposits", "cost"],
    }
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    result = {}
    for row in r.json().get("rows", []):
        cid = int(row.get("campaign_id", 0))
        result[cid] = {
            "sales":    int(row.get("sales",    0) or 0),
            "deposits": int(row.get("deposits", 0) or 0),
            "cost":     float(row.get("cost",   0) or 0),
        }
    return result

# ─── TELEGRAM ─────────────────────────────────────────────────────────────────
def tg_api(method: str, **kwargs):
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/{method}"
    r = requests.post(url, json=kwargs, timeout=15)
    r.raise_for_status()
    return r.json()

def tg_send(chat_id, text: str, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg_api("sendMessage", **payload)

def tg_edit(chat_id, message_id, text: str, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        tg_api("editMessageText", **payload)
    except:
        pass

def answer_callback(callback_query_id: str, text: str = ""):
    tg_api("answerCallbackQuery", callback_query_id=callback_query_id, text=text)

def main_keyboard():
    return {
        "keyboard": [
            [{"text": "🔍 Проверить капы"}],
            [{"text": "⚙️ Изменить настройки"}],
        ],
        "resize_keyboard": True,
    }

# ─── ПРОВЕРКА КАП ─────────────────────────────────────────────────────────────
def check_caps_for_user(buyer_notion_id: str, keitaro_group: str):
    streams = get_streams_for_buyer(buyer_notion_id)
    if not streams:
        return "📭 Потоков не найдено (статус Запущен/Не запущен/Холд)", []

    campaigns = get_campaigns_by_group(keitaro_group)
    ln_to_cids = {}
    for c in campaigns:
        m = re.match(r"^(LN-\d+)", c.get("name", ""))
        if m:
            ln = m.group(1)
            ln_to_cids.setdefault(ln, []).append(int(c["id"]))

    all_cids = [cid for s in streams for cid in ln_to_cids.get(s["ln_id"], [])]
    stats_90d = get_stats(all_cids, days=90)
    stats_7d  = get_stats(all_cids, days=NO_TRAFFIC_DAYS)

    lines = [f"📊 <b>Отчёт по капам</b> — {keitaro_group}\n"]
    actions = []

    for s in streams:
        ln_id = s["ln_id"]
        cap   = s["cap"]
        cids  = ln_to_cids.get(ln_id, [])

        if not cids:
            lines.append(f"⚪ <b>{ln_id}</b> — нет в Keitaro")
            continue

        total_90d = sum(stats_90d.get(c, {}).get("sales", 0) + stats_90d.get(c, {}).get("deposits", 0) for c in cids)
        cost_7d   = sum(stats_7d.get(c, {}).get("cost", 0) for c in cids)
        remaining = cap - total_90d
        pct       = total_90d / cap if cap else 0

        link = f'<a href="{s["notion_url"]}">{ln_id}</a>'

        if pct >= 1.0:
            overflow = total_90d - cap
            lines.append(f"🔴 {link} — <b>ПЕРЕЛИВ на {overflow} FD</b> ({total_90d}/{cap}) — нужен стоп!")
            actions.append({"ln_id": ln_id, "notion_id": s["notion_id"], "action": "stop"})
        elif cost_7d == 0:
            lines.append(f"⚠️ {link} — <b>не льётся {NO_TRAFFIC_DAYS} дней</b> → поставить холд?")
            actions.append({"ln_id": ln_id, "notion_id": s["notion_id"], "action": "hold"})
        elif remaining <= 0:
            lines.append(f"🟡 {link} — капа закрыта ({total_90d}/{cap})")
        else:
            lines.append(f"✅ {link} — ещё <b>{remaining} FD</b> ({total_90d}/{cap})")

    return "\n".join(lines), actions

def build_actions_keyboard(actions):
    buttons = []
    for a in actions:
        if a["action"] == "stop":
            buttons.append([{"text": f"⛔ Стоп {a['ln_id']}", "callback_data": f"stop:{a['notion_id']}:{a['ln_id']}"}])
        elif a["action"] == "hold":
            buttons.append([{"text": f"❄️ Холд {a['ln_id']}", "callback_data": f"hold:{a['notion_id']}:{a['ln_id']}"}])
    return {"inline_keyboard": buttons} if buttons else None

# ─── СОСТОЯНИЯ ────────────────────────────────────────────────────────────────
STATE_IDLE      = "idle"
STATE_ASK_TAG   = "ask_tag"

# ─── ОБРАБОТКА СООБЩЕНИЙ ──────────────────────────────────────────────────────
def handle_message(msg: dict, users: dict, states: dict):
    chat_id = str(msg["chat"]["id"])
    text    = msg.get("text", "").strip()
    state   = states.get(chat_id, STATE_IDLE)

    if text == "/start":
        user = users.get(chat_id)
        if user:
            tg_send(chat_id,
                f"👋 С возвращением, <b>{user['tag']}</b>!\n\n"
                f"Нажми кнопку чтобы проверить капы.",
                reply_markup=main_keyboard()
            )
        else:
            states[chat_id] = STATE_ASK_TAG
            tg_send(chat_id,
                "👋 Привет! Я бот для проверки кап.\n\n"
                "Введи свой тег (например: <code>tetriss_mb</code>):"
            )
        return

    if text == "⚙️ Изменить настройки":
        states[chat_id] = STATE_ASK_TAG
        tg_send(chat_id, "Введи свой тег:")
        return

    if text == "🔍 Проверить капы":
        user = users.get(chat_id)
        if not user:
            states[chat_id] = STATE_ASK_TAG
            tg_send(chat_id, "Сначала введи свой тег:")
            return
        tg_send(chat_id, "⏳ Загружаю данные...")
        try:
            report, actions = check_caps_for_user(user["notion_id"], user["tag"])
            keyboard = build_actions_keyboard(actions)
            tg_send(chat_id, report, reply_markup=keyboard)
        except Exception as e:
            log.error(f"check_caps error: {e}")
            tg_send(chat_id, f"❌ Ошибка: {e}")
        return

    if state == STATE_ASK_TAG:
        buyer = resolve_buyer(text)
        if not buyer:
            known = ", ".join(f"<code>{t}</code>" for t in BUYERS.keys())
            tg_send(chat_id,
                f"❌ Тег <b>{text}</b> не найден.\n"
                f"Доступные теги: {known}\n\n"
                f"Попробуй ещё раз:"
            )
            return
        users[chat_id] = {
            "tag":       buyer["name"],
            "notion_id": buyer["id"],
        }
        states[chat_id] = STATE_IDLE
        save_users(users)
        tg_send(chat_id,
            f"✅ Готово! Тег: <b>{buyer['name']}</b>\n\n"
            f"Нажми <b>🔍 Проверить капы</b>",
            reply_markup=main_keyboard()
        )
        return

    tg_send(chat_id, "Используй кнопки 👇", reply_markup=main_keyboard())


def handle_callback(cb: dict, users: dict):
    chat_id           = str(cb["message"]["chat"]["id"])
    message_id        = cb["message"]["message_id"]
    callback_query_id = cb["id"]
    data              = cb.get("data", "")

    parts = data.split(":", 2)
    if len(parts) != 3:
        answer_callback(callback_query_id, "Неизвестное действие")
        return

    action, notion_id, ln_id = parts
    status_map = {"stop": "Стопнут", "hold": "Холд"}
    emoji_map  = {"stop": "⛔", "hold": "❄️"}

    if action in status_map:
        try:
            set_notion_status(notion_id, status_map[action])
            answer_callback(callback_query_id, f"{emoji_map[action]} {ln_id} → {status_map[action]}")
            original_text = cb["message"].get("text", "")
            existing_buttons = cb["message"].get("reply_markup", {}).get("inline_keyboard", [])
            new_buttons = [
                row for row in existing_buttons
                if not any(btn.get("callback_data", "").startswith(f"{action}:{notion_id}") for btn in row)
            ]
            tg_edit(
                chat_id, message_id,
                original_text + f"\n\n✅ <b>{ln_id}</b> → <b>{status_map[action]}</b>",
                reply_markup={"inline_keyboard": new_buttons} if new_buttons else None
            )
        except Exception as e:
            answer_callback(callback_query_id, f"Ошибка: {e}")

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    log.info("🤖 Cap Bot запущен")
    users  = load_users()
    states = {}
    offset = 0

    while True:
        try:
            result  = tg_api("getUpdates", offset=offset, timeout=20, allowed_updates=["message", "callback_query"])
            updates = result.get("result", [])
            for upd in updates:
                offset = upd["update_id"] + 1
                if "message" in upd:
                    try:
                        handle_message(upd["message"], users, states)
                    except Exception as e:
                        log.error(f"handle_message error: {e}")
                elif "callback_query" in upd:
                    try:
                        handle_callback(upd["callback_query"], users)
                    except Exception as e:
                        log.error(f"handle_callback error: {e}")
        except requests.exceptions.Timeout:
            pass
        except Exception as e:
            log.error(f"polling error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
