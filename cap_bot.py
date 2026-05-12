#!/usr/bin/env python3
"""Cap Bot — проверка кап через офферы Keitaro"""

import os, re, json, time, logging, requests, sys
from datetime import datetime, timedelta

def require_env(name):
    val = os.environ.get(name)
    if not val:
        print(f"No {name}")
        sys.exit(1)
    return val

KEITARO_URL  = require_env("KEITARO_URL")
KEITARO_KEY  = require_env("KEITARO_KEY")
NOTION_TOKEN = require_env("NOTION_TOKEN")
NOTION_DB_ID = require_env("NOTION_DB_ID")
TG_BOT_TOKEN = require_env("TG_BOT_TOKEN")

USERS_FILE      = "cap_bot_users.json"
NO_TRAFFIC_DAYS = 7

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("cap_bot.log", encoding="utf-8")]
)
log = logging.getLogger(__name__)

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

def load_users():
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_users(users):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2, ensure_ascii=False)

def parse_cap(raw):
    if not raw:
        return None
    raw = raw.strip().upper()
    nums = re.findall(r"\d+", raw)
    if not nums:
        return None
    val = int(nums[-1])
    if raw.startswith("D"):
        return {"type": "daily", "value": val}
    return {"type": "total", "value": val}

# ─── NOTION ───────────────────────────────────────────────────────────────────
def get_all_streams():
    """Берём ВСЕ потоки с нужными статусами."""
    url = f"https://api.notion.com/v1/databases/{NOTION_DB_ID}/query"
    payload = {
        "filter": {
            "or": [
                {"property": "Баер статус", "select": {"equals": "Запущен"}},
                {"property": "Баер статус", "select": {"equals": "Не запущен"}},
                {"property": "Баер статус", "select": {"equals": "Холд"}},
            ]
        },
        "page_size": 100
    }
    streams = []
    while True:
        r = requests.post(url, headers=NOTION_HEADERS, json=payload, timeout=30)
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
            status = ""
            st = props.get("Баер статус", {})
            if st.get("select"):
                status = st["select"]["name"]
            streams.append({
                "notion_id":  page["id"],
                "ln_id":      ln_id,
                "cap":        parse_cap(cap_raw),
                "cap_raw":    cap_raw,
                "status":     status,
                "notion_url": page["url"],
            })
        if not data.get("has_more"):
            break
        payload["start_cursor"] = data["next_cursor"]
    log.info(f"Всего потоков из Notion: {len(streams)}")
    return streams

def set_notion_status(page_id, status):
    url = f"https://api.notion.com/v1/pages/{page_id}"
    r = requests.patch(url, headers=NOTION_HEADERS,
                       json={"properties": {"Баер статус": {"select": {"name": status}}}},
                       timeout=15)
    r.raise_for_status()

# ─── KEITARO ──────────────────────────────────────────────────────────────────
def get_offer_group_id(group_name):
    """Найти ID группы офферов по имени."""
    headers = {"Api-Key": KEITARO_KEY}
    r = requests.get(f"{KEITARO_URL}/admin_api/v1/offer_groups", headers=headers, timeout=15)
    r.raise_for_status()
    for g in r.json():
        if g.get("name", "").lower() == group_name.lower():
            return str(g["id"])
    return None

def get_offers_by_group(group_name):
    """Получить офферы из Keitaro по группе офферов."""
    headers = {"Api-Key": KEITARO_KEY}
    group_id = get_offer_group_id(group_name)
    if not group_id:
        log.warning(f"Группа офферов '{group_name}' не найдена в Keitaro")
        return []
    r = requests.get(f"{KEITARO_URL}/admin_api/v1/offers", headers=headers, timeout=15)
    r.raise_for_status()
    all_offers = r.json()
    filtered = [o for o in all_offers if str(o.get("group_id", "")) == group_id]
    log.info(f"Офферов в группе '{group_name}': {len(filtered)} из {len(all_offers)}")
    return filtered

def get_stats_by_offer(offer_ids, days=90):
    if not offer_ids:
        return {}
    url = f"{KEITARO_URL}/admin_api/v1/report/build"
    headers = {"Api-Key": KEITARO_KEY, "Content-Type": "application/json"}
    date_from = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    date_to   = datetime.now().strftime("%Y-%m-%d")
    payload = {
        "range": {"from": date_from, "to": date_to, "timezone": "Europe/Kyiv"},
        "grouping": ["offer_id"],
        "metrics": ["sales", "deposits", "cost"],
    }
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    result = {}
    for row in r.json().get("rows", []):
        oid = int(row.get("offer_id", 0))
        if oid in offer_ids:
            result[oid] = {
                "sales":    int(row.get("sales",    0) or 0),
                "deposits": int(row.get("deposits", 0) or 0),
                "cost":     float(row.get("cost",   0) or 0),
            }
    return result

def get_today_stats_by_offer(offer_ids):
    if not offer_ids:
        return {}
    url = f"{KEITARO_URL}/admin_api/v1/report/build"
    headers = {"Api-Key": KEITARO_KEY, "Content-Type": "application/json"}
    today = datetime.now().strftime("%Y-%m-%d")
    payload = {
        "range": {"from": today, "to": today, "timezone": "Europe/Kyiv"},
        "grouping": ["offer_id"],
        "metrics": ["sales", "deposits"],
    }
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    result = {}
    for row in r.json().get("rows", []):
        oid = int(row.get("offer_id", 0))
        if oid in offer_ids:
            result[oid] = {
                "sales":    int(row.get("sales",    0) or 0),
                "deposits": int(row.get("deposits", 0) or 0),
            }
    return result

# ─── TELEGRAM ─────────────────────────────────────────────────────────────────
def tg_api(method, **kwargs):
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/{method}"
    r = requests.post(url, json=kwargs, timeout=15)
    r.raise_for_status()
    return r.json()

def tg_send(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg_api("sendMessage", **payload)

def tg_edit(chat_id, message_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text,
               "parse_mode": "HTML", "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        tg_api("editMessageText", **payload)
    except Exception:
        pass

def answer_callback(cbq_id, text=""):
    tg_api("answerCallbackQuery", callback_query_id=cbq_id, text=text)

def main_keyboard():
    return {"keyboard": [[{"text": "🔍 Проверить капы"}], [{"text": "⚙️ Изменить настройки"}]],
            "resize_keyboard": True}

# ─── ОТЧЁТ ────────────────────────────────────────────────────────────────────
def check_caps_for_user(group_name):
    # 1. Все потоки из Notion
    all_streams = get_all_streams()

    # 2. Офферы из Keitaro по группе байера
    group_offers = get_offers_by_group(group_name)
    if not group_offers:
        return f"❌ Группа офферов '<b>{group_name}</b>' не найдена в Keitaro.\n\nПроверь название группы — оно должно совпадать точно (например: <code>tetriss_mb_frz</code>)", []

    # 3. Матчим LN-XXXXX из офферов
    ln_to_offer_ids = {}
    for o in group_offers:
        name = o.get("name", "")
        m = re.match(r"^(LN-\d+)", name)
        if m:
            ln = m.group(1)
            ln_to_offer_ids.setdefault(ln, []).append(int(o["id"]))

    # 4. Оставляем только потоки которые есть в офферах группы
    streams = [s for s in all_streams if s["ln_id"] in ln_to_offer_ids]
    log.info(f"Потоков с матчем в группе '{group_name}': {len(streams)}")

    if not streams:
        return f"📭 Потоков для группы '<b>{group_name}</b>' не найдено", []

    # 5. Статистика
    all_offer_ids = set(oid for s in streams for oid in ln_to_offer_ids.get(s["ln_id"], []))
    stats_all   = get_stats_by_offer(all_offer_ids, days=90)
    stats_today = get_today_stats_by_offer(all_offer_ids)
    stats_7d    = get_stats_by_offer(all_offer_ids, days=NO_TRAFFIC_DAYS)

    lines = [f"📊 <b>Отчёт по капам</b> — {group_name}\n"]
    actions = []

    for s in streams:
        ln_id     = s["ln_id"]
        cap_info  = s["cap"]
        cap_raw   = s["cap_raw"] if s["cap_raw"] else "?"
        offer_ids = ln_to_offer_ids.get(ln_id, [])
        link      = f'<a href="{s["notion_url"]}">{ln_id}</a>'

        if not cap_info:
            total = sum(stats_all.get(o, {}).get("sales", 0) +
                       stats_all.get(o, {}).get("deposits", 0) for o in offer_ids)
            lines.append(f"⚪ {link} — тотал: {total} (капа не указана)")
            continue

        cap_type = cap_info["type"]
        cap_val  = cap_info["value"]

        if cap_type == "daily":
            actual = sum(stats_today.get(o, {}).get("sales", 0) +
                        stats_today.get(o, {}).get("deposits", 0) for o in offer_ids)
            period = "сегодня"
        else:
            actual = sum(stats_all.get(o, {}).get("sales", 0) +
                        stats_all.get(o, {}).get("deposits", 0) for o in offer_ids)
            period = "тотал"

        cost_7d   = sum(stats_7d.get(o, {}).get("cost", 0) for o in offer_ids)
        remaining = cap_val - actual
        pct       = actual / cap_val if cap_val else 0

        if pct >= 1.0:
            overflow = actual - cap_val
            lines.append(f"🔴 {link} [{cap_raw}] — <b>ПЕРЕЛИВ {overflow} FD</b> ({actual}/{cap_val} {period})")
            actions.append({"ln_id": ln_id, "notion_id": s["notion_id"], "action": "stop"})
        elif cost_7d == 0:
            lines.append(f"⚠️ {link} [{cap_raw}] — не льётся {NO_TRAFFIC_DAYS} дней ({actual}/{cap_val} {period})")
            actions.append({"ln_id": ln_id, "notion_id": s["notion_id"], "action": "hold"})
        elif remaining <= 0:
            lines.append(f"🟡 {link} [{cap_raw}] — закрыта ({actual}/{cap_val} {period})")
        else:
            lines.append(f"✅ {link} [{cap_raw}] — ещё <b>{remaining} FD</b> ({actual}/{cap_val} {period})")

    return "\n".join(lines), actions

def build_keyboard(actions):
    buttons = []
    for a in actions:
        if a["action"] == "stop":
            buttons.append([{"text": f"⛔ Стоп {a['ln_id']}",
                             "callback_data": f"stop:{a['notion_id']}:{a['ln_id']}"}])
        elif a["action"] == "hold":
            buttons.append([{"text": f"❄️ Холд {a['ln_id']}",
                             "callback_data": f"hold:{a['notion_id']}:{a['ln_id']}"}])
    return {"inline_keyboard": buttons} if buttons else None

# ─── ХЭНДЛЕРЫ ─────────────────────────────────────────────────────────────────
STATE_IDLE    = "idle"
STATE_ASK_TAG = "ask_tag"

def handle_message(msg, users, states):
    chat_id = str(msg["chat"]["id"])
    text    = msg.get("text", "").strip()
    state   = states.get(chat_id, STATE_IDLE)

    if text == "/start":
        user = users.get(chat_id)
        if user:
            tg_send(chat_id,
                f"👋 С возвращением!\n"
                f"Группа Keitaro: <b>{user['group']}</b>\n\n"
                f"Нажми кнопку чтобы проверить капы.",
                reply_markup=main_keyboard())
        else:
            states[chat_id] = STATE_ASK_TAG
            tg_send(chat_id,
                "👋 Привет! Введи название своей группы офферов в Keitaro\n"
                "(например: <code>tetriss_mb_frz</code>):")
        return

    if text == "⚙️ Изменить настройки":
        states[chat_id] = STATE_ASK_TAG
        tg_send(chat_id, "Введи название группы офферов в Keitaro:")
        return

    if text == "🔍 Проверить капы":
        user = users.get(chat_id)
        if not user:
            states[chat_id] = STATE_ASK_TAG
            tg_send(chat_id, "Сначала введи свою группу в Keitaro:")
            return
        tg_send(chat_id, "⏳ Загружаю данные...")
        try:
            report, actions = check_caps_for_user(user["group"])
            tg_send(chat_id, report, reply_markup=build_keyboard(actions))
        except Exception as e:
            log.error(f"check_caps: {e}")
            tg_send(chat_id, f"❌ Ошибка: {e}")
        return

    if state == STATE_ASK_TAG:
        group = text.strip()
        if not group:
            tg_send(chat_id, "Введи название группы:")
            return
        # Проверяем что группа существует в Keitaro
        tg_send(chat_id, f"⏳ Проверяю группу <b>{group}</b> в Keitaro...")
        group_id = get_offer_group_id(group)
        if not group_id:
            tg_send(chat_id,
                f"❌ Группа <b>{group}</b> не найдена в Keitaro.\n"
                f"Проверь название и попробуй снова:")
            return
        users[chat_id] = {"group": group}
        states[chat_id] = STATE_IDLE
        save_users(users)
        tg_send(chat_id,
            f"✅ Группа сохранена: <b>{group}</b>",
            reply_markup=main_keyboard())
        return

    tg_send(chat_id, "Используй кнопки 👇", reply_markup=main_keyboard())

def handle_callback(cb, users):
    chat_id = str(cb["message"]["chat"]["id"])
    msg_id  = cb["message"]["message_id"]
    cbq_id  = cb["id"]
    parts   = cb.get("data", "").split(":", 2)
    if len(parts) != 3:
        answer_callback(cbq_id, "Ошибка")
        return
    action, notion_id, ln_id = parts
    status_map = {"stop": "Стопнут", "hold": "Холд"}
    emoji_map  = {"stop": "⛔", "hold": "❄️"}
    if action in status_map:
        try:
            set_notion_status(notion_id, status_map[action])
            answer_callback(cbq_id, f"{emoji_map[action]} {ln_id} → {status_map[action]}")
            orig     = cb["message"].get("text", "")
            existing = cb["message"].get("reply_markup", {}).get("inline_keyboard", [])
            new_btns = [row for row in existing
                       if not any(btn.get("callback_data", "").startswith(f"{action}:{notion_id}")
                                  for btn in row)]
            tg_edit(chat_id, msg_id,
                    orig + f"\n\n✅ <b>{ln_id}</b> → <b>{status_map[action]}</b>",
                    reply_markup={"inline_keyboard": new_btns} if new_btns else None)
        except Exception as e:
            answer_callback(cbq_id, f"Ошибка: {e}")

def main():
    log.info("🤖 Cap Bot запущен")
    users  = load_users()
    states = {}
    offset = 0
    while True:
        try:
            result = tg_api("getUpdates", offset=offset, timeout=20,
                            allowed_updates=["message", "callback_query"])
            for upd in result.get("result", []):
                offset = upd["update_id"] + 1
                if "message" in upd:
                    try:
                        handle_message(upd["message"], users, states)
                    except Exception as e:
                        log.error(f"handle_message: {e}")
                elif "callback_query" in upd:
                    try:
                        handle_callback(upd["callback_query"], users)
                    except Exception as e:
                        log.error(f"handle_callback: {e}")
        except requests.exceptions.Timeout:
            pass
        except Exception as e:
            log.error(f"polling: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
